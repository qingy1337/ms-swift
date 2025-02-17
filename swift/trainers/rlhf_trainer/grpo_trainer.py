# Copyright (c) Alibaba, Inc. and its affiliates.
# Part of the implementation is borrowed from huggingface/trl.
import inspect
import os
from collections import defaultdict, namedtuple
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Union
from unittest.mock import patch

import torch
import torch.nn as nn
from accelerate.utils import broadcast_object_list, gather, gather_object
from transformers import PreTrainedModel
from transformers.utils.versions import require_version
from trl import GRPOTrainer as HFGRPOTrainer
from trl.models import unwrap_model_for_generation

from swift.llm import InferRequest, RequestConfig, to_device
from swift.plugin.orm import orms
from swift.utils import (JsonlWriter, get_device, get_device_count, get_dist_setting, get_logger, is_vllm_available,
                         is_wandb_available)
from ..mixin import SwiftMixin
from .rlhf_mixin import RLHFTrainerMixin

del HFGRPOTrainer.__init__
del HFGRPOTrainer._prepare_inputs

logger = get_logger()
if is_wandb_available():
    import wandb


class GRPOTrainer(RLHFTrainerMixin, SwiftMixin, HFGRPOTrainer):

    def __init__(self,
                 model: Optional[Union[PreTrainedModel, nn.Module]] = None,
                 ref_model: Optional[Union[PreTrainedModel, nn.Module]] = None,
                 reward_model: Optional[Union[PreTrainedModel, nn.Module]] = None,
                 reward_funcs: Optional[List[Union[str, Callable]]] = None,
                 *_args,
                 **kwargs):
        require_version('trl>=0.15')
        args = kwargs['args']

        self.processing_class = kwargs.get('template').tokenizer
        if not isinstance(reward_funcs, list):
            reward_funcs = [reward_funcs]

        if reward_funcs:
            for i, reward_func in enumerate(reward_funcs):
                if reward_func in orms:
                    reward_func_class = orms[reward_func]
                    reward_func_args = list(inspect.signature(reward_func_class.__init__).parameters)
                    reward_func_args = [
                        getattr(args, param) for param in reward_func_args if param not in ['self', 'args', 'kwargs']
                    ]
                    reward_funcs[i] = reward_func_class(*reward_func_args)
                elif not callable(reward_func):
                    raise ValueError(f'reward_function {reward_func} is not implemented in swift.llm.plugin')

        self.reward_funcs = reward_funcs
        self.reward_templates = [None] * len(self.reward_funcs)
        if reward_model is not None:
            self.reward_templates.append(kwargs.pop('reward_template', None))
            self.reward_funcs.append(reward_model)
        if not self.reward_funcs:
            raise ValueError('You must specify reward_funcs or reward_model')

        # Reward weights
        if args.reward_weights is not None:
            if len(args.reward_weights) != len(reward_funcs):
                raise ValueError(f'Number of reward weights ({len(args.reward_weights)}) must match number of reward '
                                 f'functions ({len(reward_funcs)})')
            self.reward_weights = torch.tensor(args.reward_weights, dtype=torch.float32)
        else:
            self.reward_weights = torch.ones(len(reward_funcs), dtype=torch.float32)

        self.num_generations = args.num_generations
        model.warnings_issued['estimate_tokens'] = True
        kwargs['data_collator'] = lambda x: x
        self._metrics = defaultdict(list)

        use_vllm = args.use_vllm

        super().__init__(model, ref_model, *_args, **kwargs)

        num_processes = self.accelerator.num_processes
        global_batch_size = args.per_device_train_batch_size * num_processes
        possible_values = [n_gen for n_gen in range(2, global_batch_size + 1) if (global_batch_size) % n_gen == 0]
        if self.num_generations not in possible_values:
            raise ValueError(
                f'The global train batch size ({num_processes} x {args.per_device_train_batch_size}) must be evenly '
                f'divisible by the number of generations per prompt ({self.num_generations}). Given the current train '
                f'batch size, the valid values for the number of generations are: {possible_values}.')
        if self.args.eval_strategy != 'no':
            global_batch_size = args.per_device_eval_batch_size * num_processes
            possible_values = [n_gen for n_gen in range(2, global_batch_size + 1) if (global_batch_size) % n_gen == 0]
            if self.num_generations not in possible_values:
                raise ValueError(
                    f'The global eval batch size ({num_processes} x {args.per_device_eval_batch_size}) must be evenly '
                    f'divisible by the number of generations per prompt ({self.num_generations}). Given the current '
                    f'eval batch size, the valid values for the number of generations are: {possible_values}.')

        if use_vllm:
            if not is_vllm_available():
                raise ImportError('vLLM is not available and `use_vllm` is set to True. Please install vLLM with '
                                  '`pip install vllm` to use it.')
            if self.accelerator.is_main_process:
                vllm_device = self.args.vllm_device
                if vllm_device == 'auto':
                    if get_device_count() == 1:
                        vllm_device = get_device()  # particular case when training with only 1 GPU: share it
                    else:
                        local_world_size = get_dist_setting()[3]
                        vllm_device = get_device(local_world_size)  # take the next GPU idx
                # Check that the requested device is available
                if vllm_device.split(':')[0] in {'cuda', 'npu'
                                                 } and int(vllm_device.split(':')[1]) >= get_device_count():
                    raise ValueError(
                        f'The requested device for vllm ({vllm_device}) is not available. You are likely using vLLM '
                        'without restricting the number of GPUs for training. Set the `--num_processes` argument to a '
                        'value lower than the number of GPUs available on your machine—typically, reducing it by one '
                        f'is sufficient. In your case: `--num_processes {get_device_count() - 1}`.')
                # Check that the requested device is not also used for training
                if vllm_device in {get_device(idx) for idx in range(self.accelerator.num_processes)}:
                    logger.warning(
                        f'The requested device {vllm_device} is also used for training. This may lead to unexpected '
                        'behavior. It is recommended to use a dedicated device for vLLM.')
                from swift.llm import VllmEngine
                world_size_patch = patch('torch.distributed.get_world_size', return_value=1)
                profiling_patch = patch(
                    'vllm.worker.worker.Worker._assert_memory_footprint_increased_during_profiling', return_value=None)
                from swift.tuners import Swift
                with world_size_patch, profiling_patch, Swift.grpo_context(model, self.template.processor):
                    self.engine = VllmEngine(
                        model.model_dir,
                        model.model_info.torch_dtype,
                        model_type=model.model_meta.model_type,
                        device=vllm_device,
                        gpu_memory_utilization=args.vllm_gpu_memory_utilization,
                        enable_prefix_caching=args.vllm_enable_prefix_caching,
                        max_num_seqs=args.vllm_max_num_seqs,
                        enforce_eager=args.vllm_enforce_eager,
                        limit_mm_per_prompt=args.vllm_limit_mm_per_prompt,
                        max_model_len=args.vllm_max_model_len)
                    # compat _move_model_to_vllm
                    self.llm = namedtuple('LLM', ['llm_engine'])(self.engine.engine.engine)
                self.engine.default_template = self.template
            self._last_loaded_step = 0
            self.accelerator.wait_for_everyone()
        else:
            from swift.llm import PtEngine
            self.engine = PtEngine.from_model_template(self.model, self.template, max_batch_size=0)  # 0: no limit
        self.request_config = RequestConfig(
            max_tokens=args.max_completion_length,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            repetition_penalty=args.repetition_penalty,
        )

        self.model_accepts_loss_kwargs = False
        for i, reward_func in enumerate(self.reward_funcs):
            if isinstance(reward_func, PreTrainedModel):
                self.reward_funcs[i] = self.accelerator.prepare_model(reward_func, evaluation_mode=True)
        self.log_completions = args.log_completions
        self.jsonl_writer = JsonlWriter(os.path.join(self.args.output_dir, 'completions.jsonl'))

    @staticmethod
    @contextmanager
    def _template_context(template):
        # The max_length for prompt and completion has already been restricted, so there is no need for max_length here.
        max_length = template.max_length
        mode = template.mode
        if mode in {'vllm', 'pt', 'lmdeploy'}:
            template.set_mode('train')
        template.max_length = None
        try:
            yield
        finally:
            template.set_mode(mode)
            template.max_length = max_length

    def _prepare_inputs(self, inputs) -> Dict[str, Union[torch.Tensor, Any]]:
        device = self.accelerator.device

        # Generate completions using either vLLM or regular generation
        if self.args.use_vllm:
            # First, have main process load weights if needed
            if self.state.global_step != self._last_loaded_step:
                self._move_model_to_vllm()
                self._last_loaded_step = self.state.global_step

            # Generate completions using vLLM: gather all prompts and use them in a single call in the main process
            all_inputs = gather_object(inputs)
            if self.accelerator.is_main_process:
                outputs = self.engine.infer(all_inputs, self.request_config, use_tqdm=False)
            else:
                outputs = [None] * len(all_inputs)

            # Broadcast the completions from the main process to all processes, ensuring each process receives its
            # corresponding slice.
            outputs = broadcast_object_list(outputs, from_process=0)
        else:
            # Regular generation path
            is_multimodal = self.model.model_meta.is_multimodal
            if is_multimodal:
                models = self.template.remove_post_encode_hook()
            with unwrap_model_for_generation(self.model, self.accelerator):
                # same reference
                outputs = self.engine.infer(inputs, self.request_config, use_tqdm=False)
            if is_multimodal:
                self.template.register_post_encode_hook(models)

        # Slice to keep only the local part of the data
        process_slice = slice(
            self.accelerator.process_index * len(inputs),
            (self.accelerator.process_index + 1) * len(inputs),
        )
        if self.args.use_vllm:
            outputs = outputs[process_slice]

        for i, output in enumerate(outputs):
            messages = inputs[i]['messages']
            InferRequest.remove_response(messages)
            messages.append({'role': 'assistant', 'content': output.choices[0].message.content})

        with self._template_context(self.template):
            batched_inputs = [self.template.encode(infer_request) for infer_request in inputs]
            outputs = to_device(self.template.data_collator(batched_inputs), self.model.device)

        # we only need to compute the logits for the completion tokens
        labels = outputs.pop('labels')
        logits_to_keep = (labels.shape[-1] - (torch.ne(labels, -100).int().argmax(-1))).max().item()
        outputs['logits_to_keep'] = logits_to_keep
        outputs['completion_mask'] = labels[:, -logits_to_keep:] != -100

        with torch.inference_mode():
            if self.ref_model is not None:
                ref_per_token_logps = self._get_per_token_logps(self.ref_model, outputs)
            else:
                with self.accelerator.unwrap_model(self.model).disable_adapter():
                    ref_per_token_logps = self._get_per_token_logps(self.model, outputs)

        rewards_per_func = torch.zeros((len(inputs), len(self.reward_funcs)), device=device)
        completions = [example['messages'][-1]['content'] for example in inputs]

        for i, (reward_func, reward_template) in enumerate(zip(self.reward_funcs, self.reward_templates)):
            if isinstance(reward_func, nn.Module):  # Module instead of PretrainedModel for compat with compiled models
                with self._template_context(reward_template):
                    batched_inputs = [reward_template.encode(infer_request) for infer_request in inputs]
                    reward_inputs = to_device(reward_template.data_collator(batched_inputs), reward_func.device)

                with torch.inference_mode():
                    rewards_per_func[:, i] = reward_func(**reward_inputs).logits[:, 0]
            else:
                # Repeat all input columns (but "messages" and "completion") to match the number of generations
                reward_kwargs = {key: [example[key] for example in inputs] for key in inputs[0]}
                output_reward_func = reward_func(completions, **reward_kwargs)
                rewards_per_func[:, i] = torch.tensor(output_reward_func, dtype=torch.float32, device=device)

        rewards_per_func = gather(rewards_per_func)
        # Apply weights to each reward function's output and sum
        rewards = (rewards_per_func * self.reward_weights.to(device).unsqueeze(0)).sum(dim=1)

        # Compute grouped-wise rewards
        mean_grouped_rewards = rewards.view(-1, self.num_generations).mean(dim=1)
        std_grouped_rewards = rewards.view(-1, self.num_generations).std(dim=1)

        # Normalize the rewards to compute the advantages
        mean_grouped_rewards = mean_grouped_rewards.repeat_interleave(self.num_generations, dim=0)
        std_grouped_rewards = std_grouped_rewards.repeat_interleave(self.num_generations, dim=0)
        advantages = (rewards - mean_grouped_rewards) / (std_grouped_rewards + 1e-4)
        advantages = advantages[process_slice]

        # Log the metrics
        reward_per_func = rewards_per_func.mean(0)
        for i, reward_func in enumerate(self.reward_funcs):
            if isinstance(reward_func, nn.Module):  # Module instead of PretrainedModel for compat with compiled models
                reward_func_name = reward_func.config._name_or_path.split('/')[-1]
            else:
                if inspect.isfunction(reward_func):
                    reward_func_name = reward_func.__name__  # function
                else:
                    reward_func_name = reward_func.__class__.__name__  # method
            self._metrics[f'rewards/{reward_func_name}'].append(reward_per_func[i].item())

        self._metrics['reward'].append(rewards.mean().item())
        self._metrics['reward_std'].append(std_grouped_rewards.mean().item())
        outputs.update({
            'ref_per_token_logps': ref_per_token_logps,
            'advantages': advantages,
        })
        if self.log_completions and self.state.global_step % self.args.logging_steps == 0:
            # For logging
            table = {
                'step': [str(self.state.global_step)] * len(rewards),
                'messages': [inputs['messages'][:-1] for inputs in gather_object(inputs)],
                'completion': gather_object(completions),
                'reward': rewards.tolist(),
            }
            self.jsonl_writer.append(table)
            if 'wandb' in self.args.report_to and wandb.run is not None and self.accelerator.is_main_process:
                import pandas as pd
                df = pd.DataFrame(table)
                wandb.log({'completions': wandb.Table(dataframe=df)})

        return outputs

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        if return_outputs:
            raise ValueError('The GRPOTrainer does not support returning outputs')
        # Compute the per-token log probabilities for the model
        completion_mask = inputs['completion_mask']
        per_token_logps = self._get_per_token_logps(model, inputs)

        # Compute the KL divergence between the model and the reference model
        ref_per_token_logps = inputs['ref_per_token_logps']
        per_token_kl = torch.exp(ref_per_token_logps - per_token_logps) - (ref_per_token_logps - per_token_logps) - 1

        # x - x.detach() allows for preserving gradients from x
        advantages = inputs['advantages']
        per_token_loss = torch.exp(per_token_logps - per_token_logps.detach()) * advantages.unsqueeze(1)
        per_token_loss = -(per_token_loss - self.beta * per_token_kl)

        loss = ((per_token_loss * completion_mask).sum(dim=1) / completion_mask.sum(dim=1)).mean()

        # Log the metrics
        completion_length = self.accelerator.gather_for_metrics(completion_mask.sum(1)).float().mean().item()
        self._metrics['completion_length'].append(completion_length)

        mean_kl = ((per_token_kl * completion_mask).sum(dim=1) / completion_mask.sum(dim=1)).mean()
        self._metrics['kl'].append(self.accelerator.gather_for_metrics(mean_kl).mean().item())

        return loss

    # Get the per-token log probabilities for the completions for the model and the reference model
    def _get_per_token_logps(self, model, inputs):
        # pip install trl>=0.15
        from trl.trainer.utils import selective_log_softmax
        logits_to_keep = inputs['logits_to_keep']
        input_ids = inputs['input_ids']
        unwrapped_model = self.accelerator.unwrap_model(model)
        parameters = inspect.signature(unwrapped_model.forward).parameters
        if not unwrapped_model.model_meta.is_multimodal and 'logits_to_keep' in parameters:
            # save memory
            return super()._get_per_token_logps(model, input_ids, inputs['attention_mask'], logits_to_keep)
        inputs = {
            k: v
            for k, v in inputs.items()
            if k not in ['logits_to_keep', 'completion_mask', 'ref_per_token_logps', 'advantages']
        }
        logits = model(**inputs).logits
        # exclude the last logit: it corresponds to the next token pred
        logits = logits[:, -(logits_to_keep + 1):-1, :]
        input_ids = input_ids[:, -logits_to_keep:]
        return selective_log_softmax(logits, input_ids)  # compute logprobs for the input tokens
