import sys
from typing import Callable, Optional
import torch
from torch import Tensor
import math
from einops import rearrange
from torch.nn.functional import group_norm

import comfy.samplers as comfy_samplers
# import comfy.model_management as model_management
from comfy.controlnet import ControlBase

from comfy.model_patcher import ModelPatcher

from comfy.ldm.modules.attention import SpatialTransformer
import comfy.ldm.modules.diffusionmodules.openaimodel as openaimodel
import comfy.model_management as model_management

from .logger import logger

from .motion_module_ad import VanillaTemporalModule
from .motion_module import InjectionParams, eject_motion_module, inject_motion_module, inject_params_into_model, \
    load_motion_module, unload_motion_module
from .motion_module import is_injected_mm_params, get_injected_mm_params
from .motion_utils import GenericMotionWrapper, GroupNormAD
from .context import get_context_scheduler
from .model_utils import BetaScheduleCache, BetaSchedules, wrap_function_to_inject_xformers_bug_info


##################################################################################
######################################################################
# Global variable to use to more conveniently hack variable access into samplers
class AnimateDiffHelper_GlobalState:
    def __init__(self):
        self.motion_module: GenericMotionWrapper = None
        self.reset()

    def reset(self):
        self.start_step: int = 0
        self.last_step: int = 0
        self.current_step: int = 0
        self.total_steps: int = 0
        self.video_length: int = 0
        self.context_frames: Optional[int] = None
        self.context_stride: Optional[int] = None
        self.context_overlap: Optional[int] = None
        self.context_schedule: Optional[str] = None
        self.closed_loop: bool = False
        self.sync_context_to_pe: bool = False
        self.sub_idxs: list = None
        if self.motion_module is not None:
            del self.motion_module
            self.motion_module = None

    def update_with_inject_params(self, params: InjectionParams):
        self.video_length = params.video_length
        self.context_frames = params.context_length
        self.context_stride = params.context_stride
        self.context_overlap = params.context_overlap
        self.context_schedule = params.context_schedule
        self.closed_loop = params.closed_loop
        self.sync_context_to_pe = params.sync_context_to_pe

    def is_using_sliding_context(self):
        return self.context_frames is not None


ADGS = AnimateDiffHelper_GlobalState()


######################################################################
##################################################################################


##################################################################################
#### Code Injection ##################################################
def forward_timestep_embed(
        ts, x, emb, context=None, transformer_options={}, output_shape=None
):
    for layer in ts:
        if isinstance(layer, openaimodel.TimestepBlock):
            x = layer(x, emb)
        elif isinstance(layer, VanillaTemporalModule):
            x = layer(x, context)
        elif isinstance(layer, SpatialTransformer):
            x = layer(x, context, transformer_options)
            transformer_options["current_index"] += 1
        elif isinstance(layer, openaimodel.Upsample):
            x = layer(x, output_shape=output_shape)
        else:
            x = layer(x)
    return x


def unlimited_batch_area():
    return int(sys.maxsize)


def groupnorm_mm_factory(params: InjectionParams):
    def groupnorm_mm_forward(self, input_tensor: Tensor) -> Tensor:
        # axes_factor normalizes batch based on total conds and unconds passed in batch;
        # the conds and unconds per batch can change based on VRAM optimizations that may kick in
        if not ADGS.is_using_sliding_context():
            axes_factor = input_tensor.size(0) // params.video_length
        else:
            axes_factor = input_tensor.size(0) // params.context_length

        input_tensor = rearrange(input_tensor, "(b f) c h w -> b c f h w", b=axes_factor)
        input_tensor = group_norm(input_tensor, self.num_groups, self.weight, self.bias, self.eps)
        input_tensor = rearrange(input_tensor, "b c f h w -> (b f) c h w", b=axes_factor)
        return input_tensor

    return groupnorm_mm_forward


######################################################################
##################################################################################


def animatediff_sample_factory(orig_comfy_sample: Callable) -> Callable:
    def animatediff_sample(model: ModelPatcher, *args, **kwargs):
        motion_module = None
        # check if model has params - if not, no need to do anything
        if not is_injected_mm_params(model):
            return orig_comfy_sample(model, *args, **kwargs)
        # otherwise, injection time
        try:
            # get params - clone to keep from resetting values on cached model
            params = get_injected_mm_params(model).clone()
            # get amount of latents passed in, and inject into model
            latents = args[-1]
            params.video_length = latents.size(0)
            model = inject_params_into_model(model, params)
            # reset global state
            ADGS.reset()
            ##############################################
            # Save Original Functions
            orig_forward_timestep_embed = openaimodel.forward_timestep_embed  # needed to account for VanillaTemporalModule
            orig_maximum_batch_area = model_management.maximum_batch_area  # allows for "unlimited area hack" to prevent halving of conds/unconds
            orig_groupnorm_forward = torch.nn.GroupNorm.forward  # used to normalize latents to remove "flickering" of colors/brightness between frames
            orig_groupnormad_forward = GroupNormAD.forward
            orig_sampling_function = comfy_samplers.sampling_function  # used to support sliding context windows in samplers
            # save original beta schedule settings
            orig_beta_cache = BetaScheduleCache(model)
            ##############################################

            ##############################################
            # Inject Functions
            openaimodel.forward_timestep_embed = forward_timestep_embed
            if params.unlimited_area_hack:
                model_management.maximum_batch_area = unlimited_batch_area
            torch.nn.GroupNorm.forward = groupnorm_mm_factory(params)
            if params.apply_mm_groupnorm_hack:
                GroupNormAD.forward = groupnorm_mm_factory(params)
            comfy_samplers.sampling_function = sliding_sampling_function
            ##############################################

            # try to load motion module
            motion_module = load_motion_module(params.model_name, params.loras, model=model, motion_model_settings=params.motion_model_settings)
            # inject motion module into unet
            inject_motion_module(model=model, motion_module=motion_module, params=params)

            # apply suggested beta schedule
            beta_schedule = BetaSchedules.to_name(params.beta_schedule)
            model.model.register_schedule(given_betas=None, beta_schedule=beta_schedule, timesteps=1000,
                                          linear_start=0.00085, linear_end=0.012, cosine_s=8e-3)

            # apply scale multiplier, if needed
            motion_module.set_scale_multiplier(params.motion_model_settings.attn_scale)

            # handle GLOBALSTATE vars and step tally
            ADGS.motion_module = motion_module
            ADGS.update_with_inject_params(params)
            ADGS.start_step = kwargs.get("start_step") or 0
            ADGS.current_step = ADGS.start_step
            ADGS.last_step = kwargs.get("last_step") or 0

            original_callback = kwargs.get("callback", None)

            def ad_callback(step, x0, x, total_steps):
                if original_callback is not None:
                    original_callback(step, x0, x, total_steps)
                # update GLOBALSTATE for next iteration
                ADGS.current_step = ADGS.start_step + step + 1

            kwargs["callback"] = ad_callback

            return wrap_function_to_inject_xformers_bug_info(orig_comfy_sample)(model, *args, **kwargs)
        finally:
            # attempt to eject motion module
            eject_motion_module(model=model)
            # reset motion module scale multiplier
            motion_module.reset_scale_multiplier()
            # reset motion module sub_idxs
            motion_module.set_sub_idxs(None)
            # if loras are present, remove model so it can be re-loaded next time with fresh weights
            if motion_module.has_loras():
                unload_motion_module(motion_module)
                del motion_module
            ##############################################
            # Restoration
            model_management.maximum_batch_area = orig_maximum_batch_area
            openaimodel.forward_timestep_embed = orig_forward_timestep_embed
            torch.nn.GroupNorm.forward = orig_groupnorm_forward
            GroupNormAD.forward = orig_groupnormad_forward
            comfy_samplers.sampling_function = orig_sampling_function
            # reapply previous beta schedule
            orig_beta_cache.use_cached_beta_schedule_and_clean(model)
            # reset global state
            ADGS.reset()
            ##############################################

    return animatediff_sample


def sliding_sampling_function(model_function, x, timestep, uncond, cond, cond_scale, model_options={}, seed=None):
    def get_area_and_mult(conds, x_in, timestep_in):
        area = (x_in.shape[2], x_in.shape[3], 0, 0)
        strength = 1.0

        if 'timestep_start' in conds:
            timestep_start = conds['timestep_start']
            if timestep_in[0] > timestep_start:
                return None
        if 'timestep_end' in conds:
            timestep_end = conds['timestep_end']
            if timestep_in[0] < timestep_end:
                return None
        if 'area' in conds:
            area = conds['area']
        if 'strength' in conds:
            strength = conds['strength']

        input_x = x_in[:, :, area[2]:area[0] + area[2], area[3]:area[1] + area[3]]
        if 'mask' in conds:
            # Scale the mask to the size of the input
            # The mask should have been resized as we began the sampling process
            mask_strength = 1.0
            if "mask_strength" in conds:
                mask_strength = conds["mask_strength"]
            mask = conds['mask']
            assert (mask.shape[1] == x_in.shape[2])
            assert (mask.shape[2] == x_in.shape[3])
            mask = mask[:, area[2]:area[0] + area[2], area[3]:area[1] + area[3]] * mask_strength
            mask = mask.unsqueeze(1).repeat(input_x.shape[0] // mask.shape[0], input_x.shape[1], 1, 1)
        else:
            mask = torch.ones_like(input_x)
        mult = mask * strength

        if 'mask' not in conds:
            rr = 8
            if area[2] != 0:
                for t in range(rr):
                    mult[:, :, t:1 + t, :] *= ((1.0 / rr) * (t + 1))
            if (area[0] + area[2]) < x_in.shape[2]:
                for t in range(rr):
                    mult[:, :, area[0] - 1 - t:area[0] - t, :] *= ((1.0 / rr) * (t + 1))
            if area[3] != 0:
                for t in range(rr):
                    mult[:, :, :, t:1 + t] *= ((1.0 / rr) * (t + 1))
            if (area[1] + area[3]) < x_in.shape[3]:
                for t in range(rr):
                    mult[:, :, :, area[1] - 1 - t:area[1] - t] *= ((1.0 / rr) * (t + 1))

        conditionning = {}
        model_conds = conds["model_conds"]
        for c in model_conds:
            conditionning[c] = model_conds[c].process_cond(batch_size=x_in.shape[0], device=x_in.device, area=area)

        control = None
        if 'control' in conds:
            control = conds['control']

        patches = None
        if 'gligen' in conds:
            gligen = conds['gligen']
            patches = {}
            gligen_type = gligen[0]
            gligen_model = gligen[1]
            if gligen_type == "position":
                gligen_patch = gligen_model.model.set_position(input_x.shape, gligen[2], input_x.device)
            else:
                gligen_patch = gligen_model.model.set_empty(input_x.shape, input_x.device)

            patches['middle_patch'] = [gligen_patch]

        return input_x, mult, conditionning, area, control, patches

    def cond_equal_size(c1, c2):
        if c1 is c2:
            return True
        if c1.keys() != c2.keys():
            return False
        for k in c1:
            if not c1[k].can_concat(c2[k]):
                return False
        return True

    def can_concat_cond(c1, c2):
        if c1[0].shape != c2[0].shape:
            return False

        # control
        if (c1[4] is None) != (c2[4] is None):
            return False
        if c1[4] is not None:
            if c1[4] is not c2[4]:
                return False

        # patches
        if (c1[5] is None) != (c2[5] is None):
            return False
        if c1[5] is not None:
            if c1[5] is not c2[5]:
                return False

        return cond_equal_size(c1[2], c2[2])

    def cond_cat(c_list):
        c_crossattn = []
        c_concat = []
        c_adm = []
        crossattn_max_len = 0

        temp = {}
        for x_t in c_list:
            for k in x_t:
                cur = temp.get(k, [])
                cur.append(x_t[k])
                temp[k] = cur

        out = {}
        for k in temp:
            conds = temp[k]
            out[k] = conds[0].concat(conds[1:])

        return out

    def calc_cond_uncond_batch(model_function, cond, uncond, x_in, timestep, max_total_area, model_options):
        out_cond = torch.zeros_like(x_in)
        out_count = torch.ones_like(x_in) / 100000.0

        out_uncond = torch.zeros_like(x_in)
        out_uncond_count = torch.ones_like(x_in) / 100000.0

        COND = 0
        UNCOND = 1

        to_run = []
        for x_t in cond:
            p = get_area_and_mult(x_t, x_in, timestep)
            if p is None:
                continue

            to_run += [(p, COND)]
        if uncond is not None:
            for x_t in uncond:
                p = get_area_and_mult(x_t, x_in, timestep)
                if p is None:
                    continue

                to_run += [(p, UNCOND)]

        while len(to_run) > 0:
            first = to_run[0]
            first_shape = first[0][0].shape
            to_batch_temp = []
            for x_t in range(len(to_run)):
                if can_concat_cond(to_run[x_t][0], first[0]):
                    to_batch_temp += [x_t]

            to_batch_temp.reverse()
            to_batch = to_batch_temp[:1]

            for i in range(1, len(to_batch_temp) + 1):
                batch_amount = to_batch_temp[:len(to_batch_temp) // i]
                if len(batch_amount) * first_shape[0] * first_shape[2] * first_shape[3] < max_total_area:
                    to_batch = batch_amount
                    break

            input_x = []
            mult = []
            c = []
            cond_or_uncond = []
            area = []
            control = None
            patches = None
            for x_t in to_batch:
                o = to_run.pop(x_t)
                p = o[0]
                input_x += [p[0]]
                mult += [p[1]]
                c += [p[2]]
                area += [p[3]]
                cond_or_uncond += [o[1]]
                control = p[4]
                patches = p[5]

            batch_chunks = len(cond_or_uncond)
            input_x = torch.cat(input_x)
            c = cond_cat(c)
            timestep_ = torch.cat([timestep] * batch_chunks)

            if control is not None:
                c['control'] = control.get_control(input_x, timestep_, c, len(cond_or_uncond))

            transformer_options = {}
            if 'transformer_options' in model_options:
                transformer_options = model_options['transformer_options'].copy()

            if patches is not None:
                if "patches" in transformer_options:
                    cur_patches = transformer_options["patches"].copy()
                    for p in patches:
                        if p in cur_patches:
                            cur_patches[p] = cur_patches[p] + patches[p]
                        else:
                            cur_patches[p] = patches[p]
                else:
                    transformer_options["patches"] = patches

            transformer_options["cond_or_uncond"] = cond_or_uncond[:]
            c['transformer_options'] = transformer_options

            if 'model_function_wrapper' in model_options:
                output = model_options['model_function_wrapper'](model_function,
                                                                 {"input": input_x, "timestep": timestep_, "c": c,
                                                                  "cond_or_uncond": cond_or_uncond}).chunk(batch_chunks)
            else:
                output = model_function(input_x, timestep_, **c).chunk(batch_chunks)
            del input_x

            for o in range(batch_chunks):
                if cond_or_uncond[o] == COND:
                    out_cond[:, :, area[o][2]:area[o][0] + area[o][2],
                    area[o][3]:area[o][1] + area[o][3]] += output[o] * mult[o]
                    out_count[:, :, area[o][2]:area[o][0] + area[o][2], area[o][3]:area[o][1] + area[o][3]] += mult[o]
                else:
                    output = model_function(input_x, timestep_, **c).chunk(batch_chunks)
                del input_x

                for o in range(batch_chunks):
                    if cond_or_uncond[o] == COND:
                        out_cond[:,:,area[o][2]:area[o][0] + area[o][2],area[o][3]:area[o][1] + area[o][3]] += output[o] * mult[o]
                        out_count[:,:,area[o][2]:area[o][0] + area[o][2],area[o][3]:area[o][1] + area[o][3]] += mult[o]
                    else:
                        out_uncond[:,:,area[o][2]:area[o][0] + area[o][2],area[o][3]:area[o][1] + area[o][3]] += output[o] * mult[o]
                        out_uncond_count[:,:,area[o][2]:area[o][0] + area[o][2],area[o][3]:area[o][1] + area[o][3]] += mult[o]
                del mult

            out_cond /= out_count
            del out_count
            out_uncond /= out_uncond_count
            del out_uncond_count

            return out_cond, out_uncond
        
    # sliding_calc_cond_uncond_batch inspired by ashen's initial hack for 16-frame sliding context:
    # https://github.com/comfyanonymous/ComfyUI/compare/master...ashen-sensored:ComfyUI:master
    def sliding_calc_cond_uncond_batch(model_function, cond, uncond, x_in, timestep, max_total_area, model_options):
        # get context scheduler
        context_scheduler = get_context_scheduler(ADGS.context_schedule)
        # figure out how input is split
        axes_factor = x.size(0)//ADGS.video_length

        # prepare final cond, uncond, and out_count
        cond_final = torch.zeros_like(x)
        uncond_final = torch.zeros_like(x)
        out_count_final = torch.zeros((x.shape[0], 1, 1, 1), device=x.device)

        def prepare_control_objects(control: ControlBase, full_idxs: list[int]):
            if control.previous_controlnet is not None:
                prepare_control_objects(control.previous_controlnet, full_idxs)
            control.sub_idxs = full_idxs
            control.full_latent_length = ADGS.video_length
            control.context_length = ADGS.context_frames

        def get_resized_cond(cond_in, full_idxs) -> list:
            # reuse or resize cond items to match context requirements
            resized_cond = []
            # cond object is a list containing a dict - outer list is irrelevant, so just loop through it
            for actual_cond in cond_in:
                resized_actual_cond = actual_cond.copy()
                # now we are in the inner dict - "pooled_output" is a tensor, "control" is a ControlBase object, "model_conds" is dictionary
                for key in actual_cond:
                    try:
                        cond_item = actual_cond[key]
                        if isinstance(cond_item, Tensor):
                            # check that tensor is the expected length - x.size(0)
                            if cond_item.size(0) == x.size(0):
                                # if so, it's subsetting time - tell controls the expected indeces so they can handle them
                                actual_cond_item = cond_item[full_idxs]
                                resized_actual_cond[key] = actual_cond_item
                            else:
                                resized_actual_cond[key] = cond_item
                        # look for control
                        elif key == "control":
                            control_item = cond_item
                            if hasattr(control_item, "sub_idxs"):
                                prepare_control_objects(control_item, full_idxs)
                            else:
                                raise ValueError(f"Control type {type(control_item).__name__} may not support required features for sliding context window; \
                                                    use Control objects from Kosinkadink/Advanced-ControlNet nodes, or make sure Advanced-ControlNet is updated.")
                            resized_actual_cond[key] = control_item
                            del control_item
                        elif isinstance(cond_item, dict):
                            new_cond_item = cond_item.copy()
                            # when in dictionary, look for tensors and CONDCrossAttn [comfy/conds.py] (has cond attr that is a tensor)
                            for cond_key, cond_value in new_cond_item.items():
                                if isinstance(cond_value, Tensor):
                                    if cond_value.size(0) == x.size(0):
                                        new_cond_item[cond_key] = cond_value[full_idxs]
                                # if has cond that is a Tensor, check if needs to be subset
                                elif hasattr(cond_value, "cond") and isinstance(cond_value.cond, Tensor):
                                    if cond_value.cond.size(0) == x.size(0):
                                        new_cond_item[cond_key] = cond_value._copy_with(cond_value.cond[full_idxs])
                            resized_actual_cond[key] = new_cond_item
                        else:
                            resized_actual_cond[key] = cond_item
                    finally:
                        del cond_item  # just in case to prevent VRAM issues
                resized_cond.append(resized_actual_cond)
            return resized_cond

        # perform calc_cond_uncond_batch per context window
        for ctx_idxs in context_scheduler(ADGS.current_step, ADGS.total_steps, ADGS.video_length, ADGS.context_frames, ADGS.context_stride, ADGS.context_overlap, ADGS.closed_loop):
            # idxs of positional encoders in motion module to use, if needed (experimental, so disabled for now)
            if ADGS.sync_context_to_pe:
                ADGS.sub_idxs = ctx_idxs
                ADGS.motion_module.set_sub_idxs(ADGS.sub_idxs)
            # account for all portions of input frames
            full_idxs = []
            for n in range(axes_factor):
                for ind in ctx_idxs:
                    full_idxs.append((ADGS.video_length*n)+ind)
            # get subsections of x, timestep, cond, uncond, cond_concat
            sub_x = x[full_idxs]
            sub_timestep = timestep[full_idxs]
            sub_cond = get_resized_cond(cond, full_idxs) if cond is not None else None
            sub_uncond = get_resized_cond(uncond, full_idxs) if uncond is not None else None

            sub_cond_out, sub_uncond_out = calc_cond_uncond_batch(model_function, sub_cond, sub_uncond, sub_x, sub_timestep, max_total_area, model_options)

            cond_final[full_idxs] += sub_cond_out
            uncond_final[full_idxs] += sub_uncond_out
            out_count_final[full_idxs] += 1 # increment which indeces were used

        # normalize cond and uncond via division by context usage counts
        cond_final /= out_count_final
        uncond_final /= out_count_final
        return cond_final, uncond_final

        max_total_area = model_management.maximum_batch_area()
        if math.isclose(cond_scale, 1.0):
            uncond = None

        if not ADGS.is_using_sliding_context():
            cond, uncond = calc_cond_uncond_batch(model_function, cond, uncond, x, timestep, max_total_area, model_options)
        else:
            cond, uncond = sliding_calc_cond_uncond_batch(model_function, cond, uncond, x, timestep, max_total_area, model_options)
        if "sampler_cfg_function" in model_options:
            args = {"cond": cond, "uncond": uncond, "cond_scale": cond_scale, "timestep": timestep}
            return model_options["sampler_cfg_function"](args)
        else:
            return uncond + (cond - uncond) * cond_scale
