# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""Loss functions."""

import numpy as np
import torch
from torch_utils import training_stats
from torch_utils.ops import conv2d_gradfix
from torch_utils.ops import upfirdn2d

#----------------------------------------------------------------------------

class Loss:
    def accumulate_gradients(self, phase, real_img, real_c, gen_z, gen_c, gain, cur_nimg): # to be overridden by subclass
        raise NotImplementedError()

#----------------------------------------------------------------------------

class StyleGAN2Loss(Loss):
    def __init__(self, device, G, D, augment_pipe=None, r1_gamma=10, style_mixing_prob=0, pl_weight=0, pl_batch_shrink=2, pl_decay=0.01, pl_no_weight_grad=False, blur_init_sigma=0, blur_fade_kimg=0):
        super().__init__()
        self.device             = device
        self.G                  = G
        self.D                  = D
        self.augment_pipe       = augment_pipe
        self.r1_gamma           = r1_gamma
        self.style_mixing_prob  = style_mixing_prob
        self.pl_weight          = pl_weight
        self.pl_batch_shrink    = pl_batch_shrink
        self.pl_decay           = pl_decay
        self.pl_no_weight_grad  = pl_no_weight_grad
        self.pl_mean            = torch.zeros([], device=device)
        self.blur_init_sigma    = blur_init_sigma
        self.blur_fade_kimg     = blur_fade_kimg

    def run_G(self, z, c, update_emas=False):
        ws = self.G.mapping(z, c, update_emas=update_emas)
        if self.style_mixing_prob > 0:
            with torch.autograd.profiler.record_function('style_mixing'):
                cutoff = torch.empty([], dtype=torch.int64, device=ws.device).random_(1, ws.shape[1])
                cutoff = torch.where(torch.rand([], device=ws.device) < self.style_mixing_prob, cutoff, torch.full_like(cutoff, ws.shape[1]))
                ws[:, cutoff:] = self.G.mapping(torch.randn_like(z), c, update_emas=False)[:, cutoff:]
        img = self.G.synthesis(ws, update_emas=update_emas)
        return img, ws

    def run_D(self, img, c, blur_sigma=0, update_emas=False):
        blur_size = np.floor(blur_sigma * 3)
        if blur_size > 0:
            with torch.autograd.profiler.record_function('blur'):
                f = torch.arange(-blur_size, blur_size + 1, device=img.device).div(blur_sigma).square().neg().exp2()
                img = upfirdn2d.filter2d(img, f / f.sum())
        if self.augment_pipe is not None:
            img = self.augment_pipe(img)
        logits = self.D(img, c, update_emas=update_emas)
        return logits

    def accumulate_gradients(self, phase, real_img, real_c, gen_z, gen_c, gain, cur_nimg):
        loss_Gmain_value = None
        loss_Gpl_value = None
        loss_Dgen_value = None
        loss_Dreal_value = None
        loss_Dr1_value = None
        assert phase in ['Gmain', 'Greg', 'Gboth', 'Dmain', 'Dreg', 'Dboth']
        if self.pl_weight == 0:
            phase = {'Greg': 'none', 'Gboth': 'Gmain'}.get(phase, phase)
        if self.r1_gamma == 0:
            phase = {'Dreg': 'none', 'Dboth': 'Dmain'}.get(phase, phase)
        blur_sigma = max(1 - cur_nimg / (self.blur_fade_kimg * 1e3), 0) * self.blur_init_sigma if self.blur_fade_kimg > 0 else 0

        # Gmain: Maximize logits for generated images.
        if phase in ['Gmain', 'Gboth']:
            with torch.autograd.profiler.record_function('Gmain_forward'):
                gen_img, _gen_ws = self.run_G(gen_z, gen_c)
                gen_logits = self.run_D(gen_img, gen_c, blur_sigma=blur_sigma)
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Gmain = torch.nn.functional.softplus(-gen_logits) # -log(sigmoid(gen_logits))
                loss_Gmain_value = loss_Gmain.mean().item()
                training_stats.report('Loss/G/loss', loss_Gmain)
            with torch.autograd.profiler.record_function('Gmain_backward'):
                loss_Gmain.mean().mul(gain).backward()

        # Gpl: Apply path length regularization.
        if phase in ['Greg', 'Gboth']:
            with torch.autograd.profiler.record_function('Gpl_forward'):
                batch_size = gen_z.shape[0] // self.pl_batch_shrink
                gen_img, gen_ws = self.run_G(gen_z[:batch_size], gen_c[:batch_size])
                pl_noise = torch.randn_like(gen_img) / np.sqrt(gen_img.shape[2] * gen_img.shape[3])
                with torch.autograd.profiler.record_function('pl_grads'), conv2d_gradfix.no_weight_gradients(self.pl_no_weight_grad):
                    pl_grads = torch.autograd.grad(outputs=[(gen_img * pl_noise).sum()], inputs=[gen_ws], create_graph=True, only_inputs=True)[0]
                pl_lengths = pl_grads.square().sum(2).mean(1).sqrt()
                pl_mean = self.pl_mean.lerp(pl_lengths.mean(), self.pl_decay)
                self.pl_mean.copy_(pl_mean.detach())
                pl_penalty = (pl_lengths - pl_mean).square()
                training_stats.report('Loss/pl_penalty', pl_penalty)
                loss_Gpl = pl_penalty * self.pl_weight
                loss_Gpl_value = loss_Gpl.mean().item()
                training_stats.report('Loss/G/reg', loss_Gpl)
            with torch.autograd.profiler.record_function('Gpl_backward'):
                loss_Gpl.mean().mul(gain).backward()

        # Dmain: Minimize logits for generated images.
        loss_Dgen = 0
        if phase in ['Dmain', 'Dboth']:
            with torch.autograd.profiler.record_function('Dgen_forward'):
                gen_img, _gen_ws = self.run_G(gen_z, gen_c, update_emas=True)
                gen_logits = self.run_D(gen_img, gen_c, blur_sigma=blur_sigma, update_emas=True)
                training_stats.report('Loss/scores/fake', gen_logits)
                training_stats.report('Loss/signs/fake', gen_logits.sign())
                loss_Dgen = torch.nn.functional.softplus(gen_logits) # -log(1 - sigmoid(gen_logits))
                loss_Dgen_value = loss_Dgen.mean().item()
            with torch.autograd.profiler.record_function('Dgen_backward'):
                loss_Dgen.mean().mul(gain).backward()

        # Dmain: Maximize logits for real images.
        # Dr1: Apply R1 regularization.
        if phase in ['Dmain', 'Dreg', 'Dboth']:
            name = 'Dreal' if phase == 'Dmain' else 'Dr1' if phase == 'Dreg' else 'Dreal_Dr1'
            with torch.autograd.profiler.record_function(name + '_forward'):
                real_img_tmp = real_img.detach().requires_grad_(phase in ['Dreg', 'Dboth'])
                real_logits = self.run_D(real_img_tmp, real_c, blur_sigma=blur_sigma)
                training_stats.report('Loss/scores/real', real_logits)
                training_stats.report('Loss/signs/real', real_logits.sign())

                loss_Dreal = 0
                if phase in ['Dmain', 'Dboth']:
                    loss_Dreal = torch.nn.functional.softplus(-real_logits) # -log(sigmoid(real_logits))
                    loss_Dreal_value = loss_Dreal.mean().item()
                    training_stats.report('Loss/D/loss', loss_Dgen + loss_Dreal)

                loss_Dr1 = 0
                if phase in ['Dreg', 'Dboth']:
                    with torch.autograd.profiler.record_function('r1_grads'), conv2d_gradfix.no_weight_gradients():
                        r1_grads = torch.autograd.grad(outputs=[real_logits.sum()], inputs=[real_img_tmp], create_graph=True, only_inputs=True)[0]
                    r1_penalty = r1_grads.square().sum([1,2,3])
                    loss_Dr1 = r1_penalty * (self.r1_gamma / 2)
                    loss_Dr1_value = loss_Dr1.mean().item()
                    training_stats.report('Loss/r1_penalty', r1_penalty)
                    training_stats.report('Loss/D/reg', loss_Dr1)

            with torch.autograd.profiler.record_function(name + '_backward'):
                (loss_Dreal + loss_Dr1).mean().mul(gain).backward()

        total_penalty = sum(
            value for value in [loss_Gmain_value, loss_Gpl_value, loss_Dgen_value, loss_Dreal_value, loss_Dr1_value]
            if value is not None
        )

        training_stats.report('Loss/Total_penalty', total_penalty)

        if phase in ['Gmain', 'Gboth']:
            print(f"[Phase: {phase}] Gmain: {loss_Gmain_value}, Gpl: {loss_Gpl_value}, Total: {total_penalty:.4f}")
        if phase in ['Dmain', 'Dboth']:
            print(f"[Phase: {phase}] Dgen: {loss_Dgen_value}, Dreal: {loss_Dreal_value}, Dr1: {loss_Dr1_value}, Total: {total_penalty:.4f}")

#----------------------------------------------------------------------------

class StyleGAN2Loss_noface(StyleGAN2Loss):
    def __init__(self, device, G, D, face_detector, lambda_face_penalty=10.0, smoothing=0.99, min_lambda=0.25, **kwargs):
        super().__init__(device, G, D, **kwargs)
        self.face_detector = face_detector
        self.lambda_face_penalty = lambda_face_penalty
        self.smoothing = smoothing
        self.running_g_loss = 0.0
        self.running_d_loss = 0.0
        self.running_face_penalty = 0.0
        self.min_lambda = min_lambda

    def accumulate_gradients(self, phase, real_img, real_c, gen_z, gen_c, gain, cur_nimg):
        super().accumulate_gradients(phase, real_img, real_c, gen_z, gen_c, gain, cur_nimg)

        if phase in ['Gmain', 'Gboth', 'Dmain', 'Dboth']:
            with torch.autograd.profiler.record_function('G_D_direct_feedback'):
                gen_img, _gen_ws = self.run_G(gen_z, gen_c)
                face_probs = []

                for i, img in enumerate(gen_img):
                    try:
                        img_scaled = (img * 127.5 + 127.5).clamp(0, 255).to(torch.uint8).permute(1, 2, 0).cpu().numpy()
                        boxes, probs = self.face_detector.detect(img_scaled, landmarks=False)
                        face_probs.append(probs[0] if probs is not None and len(probs) > 0 and probs[0] is not None else 0.0)
                    except Exception as e:
                        face_probs.append(0.0)

                face_probs = torch.tensor(face_probs, dtype=torch.float32, device=self.device, requires_grad=True)
                face_penalty = torch.nn.functional.relu(face_probs - 0.5).mean()

                gen_logits = self.run_D(gen_img, gen_c)
                g_loss = torch.nn.functional.softplus(-gen_logits).mean()

                self.running_g_loss = self.smoothing * self.running_g_loss + (1 - self.smoothing) * g_loss.item()
                self.running_face_penalty = self.smoothing * self.running_face_penalty + (1 - self.smoothing) * face_penalty.item()

                if face_penalty.item() > 0:
                    self.lambda_face_penalty = max(self.running_g_loss / (face_penalty.item() + 1e-8), self.min_lambda)

                total_loss_G = g_loss + self.lambda_face_penalty * face_penalty

                if phase in ['Gmain', 'Gboth']:
                    total_loss_G.mean().mul(gain).backward()

                if phase in ['Dmain', 'Dboth']:
                    d_logits_real = self.run_D(real_img, real_c)
                    d_loss_real = torch.nn.functional.softplus(-d_logits_real).mean()

                    d_logits_fake = self.run_D(gen_img.detach(), gen_c)
                    d_loss_fake = torch.nn.functional.softplus(d_logits_fake).mean()

                    self.running_d_loss = self.smoothing * self.running_d_loss + (1 - self.smoothing) * (d_loss_real + d_loss_fake).item()

                    face_penalty_D = torch.nn.functional.relu(face_probs - 0.5).mean()

                    total_loss_D = d_loss_real + d_loss_fake + self.lambda_face_penalty * face_penalty_D
                    total_loss_D.mean().mul(gain).backward()

                face_penalty_value = face_penalty.mean().item()
                face_penalty_value_D = face_penalty_D.mean().item() if 'Dmain' in phase or 'Dboth' in phase else 0
                training_stats.report('Loss/G/face_penalty', face_penalty_value)
                training_stats.report('Loss/D/face_penalty', face_penalty_value_D)
                training_stats.report('Loss/G/total_loss', total_loss_G.mean().item())
                training_stats.report('Loss/D/total_loss', total_loss_D.mean().item() if 'Dmain' in phase or 'Dboth' in phase else 0)
                training_stats.report('Loss/G/lambda_face_penalty', self.lambda_face_penalty)
                print(f"[Phase: {phase}] G_loss: {g_loss.item():.4f}, Face Penalty (G): {face_penalty_value:.4f}, Face Penalty (D): {face_penalty_value_D:.4f}, Total Loss (G): {total_loss_G.mean().item():.4f}, Total Loss (D): {total_loss_D.mean().item() if 'Dmain' in phase or 'Dboth' in phase else 0:.4f}, Lambda Face Penalty: {self.lambda_face_penalty:.4f}")
