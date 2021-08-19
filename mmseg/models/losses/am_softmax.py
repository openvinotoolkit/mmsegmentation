import numpy as np
import torch
import torch.nn.functional as F

from mmseg.core import build_classification_loss, focal_loss
from ..builder import LOSSES
from .metric_learning_base import BaseMetricLearningLoss


@LOSSES.register_module()
class AMSoftmaxLoss(BaseMetricLearningLoss):
    """Computes the AM-Softmax loss with cos or arc margin"""
    margin_types = ['cos', 'arc']

    def __init__(self, margin_type='cos', margin=0.5, gamma=0.0, t=1.0, target_loss='ce', **kwargs):
        super(AMSoftmaxLoss, self).__init__(**kwargs)

        assert margin_type in AMSoftmaxLoss.margin_types
        self.margin_type = margin_type
        assert gamma >= 0.0
        self.gamma = gamma
        assert margin >= 0.0
        self.m = margin
        self.cos_m = np.cos(self.m)
        self.sin_m = np.sin(self.m)
        self.th = np.cos(np.pi - self.m)
        assert t >= 1
        self.t = t

        self.target_loss = build_classification_loss(target_loss)

    def _calculate(self, cos_theta, target, scale, ignore_index=-100):
        if self.margin_type == 'cos':
            phi_theta = cos_theta - self.m
        else:
            sine = torch.sqrt(1.0 - torch.pow(cos_theta, 2))
            phi_theta = cos_theta * self.cos_m - sine * self.sin_m
            phi_theta = torch.where(cos_theta > self.th, phi_theta, cos_theta - self.sin_m * self.m)

        print(phi_theta.size())
        exit()

        index = torch.zeros_like(cos_theta, dtype=torch.uint8).scatter_(1, target.detach().view(-1, 1), 1)
        output = torch.where(index, phi_theta, cos_theta)

        if self.gamma == 0 and self.t == 1.:
            out_losses = self.target_loss(scale * output, target)
        elif self.t > 1:
            h_theta = self.t - 1 + self.t * cos_theta
            support_vectors_mask = (1 - index) * \
                torch.lt(torch.masked_select(phi_theta, index).view(-1, 1).repeat(1, h_theta.shape[1]) - cos_theta, 0)
            output = torch.where(support_vectors_mask, h_theta, output)
            out_losses = self.target_loss(scale * output, target)
        else:
            out_losses = focal_loss(F.cross_entropy(scale * output, target, reduction='none'), self.gamma)

        return out_losses
