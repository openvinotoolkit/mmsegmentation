# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.


import colorsys
import importlib
import numpy as np
import random
import yaml

from ote_sdk.entities.label import Color, Domain, LabelEntity
from ote_sdk.entities.label_schema import LabelGroup, LabelGroupType, LabelSchemaEntity
from ote_sdk.entities.train_parameters import UpdateProgressCallback
from ote_sdk.usecases.reporting.time_monitor_callback import TimeMonitorCallback


class ColorPalette:
    def __init__(self, n, rng=None):
        assert n > 0

        if rng is None:
            rng = random.Random(0xACE)

        candidates_num = 100
        hsv_colors = [(1.0, 1.0, 1.0)]
        for _ in range(1, n):
            colors_candidates = [(rng.random(), rng.uniform(0.8, 1.0), rng.uniform(0.5, 1.0))
                                 for _ in range(candidates_num)]
            min_distances = [self.min_distance(hsv_colors, c) for c in colors_candidates]
            arg_max = np.argmax(min_distances)
            hsv_colors.append(colors_candidates[arg_max])

        self.palette = [Color(*self.hsv2rgb(*hsv)) for hsv in hsv_colors]

    @staticmethod
    def dist(c1, c2):
        dh = min(abs(c1[0] - c2[0]), 1 - abs(c1[0] - c2[0])) * 2
        ds = abs(c1[1] - c2[1])
        dv = abs(c1[2] - c2[2])
        return dh * dh + ds * ds + dv * dv

    @classmethod
    def min_distance(cls, colors_set, color_candidate):
        distances = [cls.dist(o, color_candidate) for o in colors_set]
        return np.min(distances)

    @staticmethod
    def hsv2rgb(h, s, v):
        return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))

    def __getitem__(self, n):
        return self.palette[n % len(self.palette)]

    def __len__(self):
        return len(self.palette)


def generate_label_schema(enumerated_label_names, ignore_name="Ignore"):
    num_label_names = len(enumerated_label_names)
    colors = ColorPalette(num_label_names) if num_label_names > 0 else []

    not_empty_labels, empty_label = [], None
    for label_id, name in enumerated_label_names:
        name = name.capitalize()
        if name == ignore_name:
            empty_label = LabelEntity(
                name=ignore_name,
                color=Color(255, 255, 255),
                is_empty=True,
                domain=Domain.SEGMENTATION,
                id=label_id)
        else:
            not_empty_labels.append(LabelEntity(
                name=name,
                color=colors[label_id],
                domain=Domain.SEGMENTATION,
                id=label_id))

    if empty_label is None:
        empty_label = LabelEntity(
            name=ignore_name,
            color=Color(255, 255, 255),
            is_empty=True,
            domain=Domain.SEGMENTATION,
            id=len(not_empty_labels))

    exclusive_group = LabelGroup(name="labels", labels=not_empty_labels, group_type=LabelGroupType.EXCLUSIVE)
    empty_group = LabelGroup(name="empty", labels=[empty_label], group_type=LabelGroupType.EMPTY_LABEL)

    label_schema = LabelSchemaEntity()
    label_schema.add_group(exclusive_group)
    label_schema.add_group(empty_group, exclusive_with=[exclusive_group])

    return label_schema


def load_template(path):
    with open(path) as f:
        template = yaml.full_load(f)
    return template


def get_task_class(path):
    module_name, class_name = path.rsplit('.', 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


# class TrainingProgressCallback(TimeMonitorCallback):
#     def __init__(self, update_progress_callback: UpdateProgressCallback):
#         super().__init__(0, 0, 0, 0, update_progress_callback=update_progress_callback)
#
#     def on_train_batch_end(self, batch, logs=None):
#         super().on_train_batch_end(batch, logs)
#         self.update_progress_callback(self.get_progress())
#
#
# class InferenceProgressCallback(TimeMonitorCallback):
#     def __init__(self, num_test_steps, update_progress_callback: UpdateProgressCallback):
#         super().__init__(
#             num_epoch=0,
#             num_train_steps=0,
#             num_val_steps=0,
#             num_test_steps=num_test_steps,
#             update_progress_callback=update_progress_callback)
#
#     def on_test_batch_end(self, batch=None, logs=None):
#         super().on_test_batch_end(batch, logs)
#         self.update_progress_callback(self.get_progress())