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

import io
import logging
import os
import json
from collections import defaultdict
from typing import Optional

import torch
from ote_sdk.configuration import cfg_helper
from ote_sdk.configuration.helper.utils import ids_to_strings
from ote_sdk.entities.datasets import DatasetEntity
from ote_sdk.entities.model import (
    ModelStatus,
    ModelEntity,
    ModelFormat,
    OptimizationMethod,
    ModelPrecision,
)
from ote_sdk.entities.optimization_parameters import default_progress_callback
from ote_sdk.entities.subset import Subset
from ote_sdk.entities.task_environment import TaskEnvironment
from ote_sdk.serialization.label_mapper import label_schema_to_bytes
from ote_sdk.usecases.tasks.interfaces.export_interface import ExportType
from ote_sdk.usecases.tasks.interfaces.optimization_interface import IOptimizationTask
from ote_sdk.usecases.tasks.interfaces.optimization_interface import OptimizationParameters
from ote_sdk.usecases.tasks.interfaces.optimization_interface import OptimizationType

from mmseg.apis import train_segmentor
from mmseg.apis.fake_input import get_fake_input
from mmseg.apis.ote.apis.segmentation import OTESegmentationInferenceTask
from mmseg.apis.ote.apis.segmentation.config_utils import prepare_for_training
from mmseg.apis.ote.apis.segmentation.configuration import OTESegmentationConfig
from mmseg.apis.ote.apis.segmentation.ote_utils import TrainingProgressCallback
from mmseg.apis.ote.extension.utils.hooks import OTELoggerHook
from mmseg.apis.train import build_val_dataloader
from mmseg.datasets import build_dataloader, build_dataset
from mmseg.integration.nncf import check_nncf_is_enabled
from mmseg.integration.nncf import is_accuracy_aware_training_set
from mmseg.integration.nncf import is_state_nncf
from mmseg.integration.nncf import wrap_nncf_model
from mmseg.integration.nncf.config import compose_nncf_config

logger = logging.getLogger(__name__)


class OTESegmentationNNCFTask(OTESegmentationInferenceTask, IOptimizationTask):
    def __init__(self, task_environment: TaskEnvironment):
        """"
        Task for compressing object detection models using NNCF.
        """
        self._val_dataloader = None
        self._compression_ctrl = None
        self._nncf_preset = "nncf_quantization"
        check_nncf_is_enabled()
        super().__init__(task_environment)

    def _set_attributes_by_hyperparams(self):
        quantization = self._hyperparams.nncf_optimization.enable_quantization
        pruning = self._hyperparams.nncf_optimization.enable_pruning
        if quantization and pruning:
            self._nncf_preset = "nncf_quantization_pruning"
            self._optimization_methods = [OptimizationMethod.QUANTIZATION, OptimizationMethod.FILTER_PRUNING]
            self._precision = [ModelPrecision.INT8]
            return
        if quantization and not pruning:
            self._nncf_preset = "nncf_quantization"
            self._optimization_methods = [OptimizationMethod.QUANTIZATION]
            self._precision = [ModelPrecision.INT8]
            return
        if not quantization and pruning:
            self._nncf_preset = "nncf_pruning"
            self._optimization_methods = [OptimizationMethod.FILTER_PRUNING]
            self._precision = [ModelPrecision.FP32]
            return
        raise RuntimeError('Not selected optimization algorithm')

    def _load_model(self, model: ModelEntity):
        # NNCF parts
        nncf_config_path = os.path.join(self._base_dir, "compression_config.json")

        with open(nncf_config_path) as nncf_config_file:
            common_nncf_config = json.load(nncf_config_file)

        self._set_attributes_by_hyperparams()

        optimization_config = compose_nncf_config(common_nncf_config, [self._nncf_preset])

        max_acc_drop = self._hyperparams.nncf_optimization.maximal_accuracy_degradation / 100
        if "accuracy_aware_training" in optimization_config["nncf_config"]:
            # Update maximal_absolute_accuracy_degradation
            (optimization_config["nncf_config"]["accuracy_aware_training"]
                                ["params"]["maximal_absolute_accuracy_degradation"]) = max_acc_drop
            # Force evaluation interval
            self._config.evaluation.interval = 1
        else:
            logger.info("NNCF config has no accuracy_aware_training parameters")

        self._config.update(optimization_config)

        compression_ctrl = None
        if model is not None:
            # If a model has been trained and saved for the task already, create empty model and load weights here
            buffer = io.BytesIO(model.get_data("weights.pth"))
            model_data = torch.load(buffer, map_location=torch.device('cpu'))

            model = self._create_model(self._config, from_scratch=True)
            try:
                if is_state_nncf(model_data):
                    compression_ctrl, model = wrap_nncf_model(
                        model,
                        self._config,
                        init_state_dict=model_data,
                        get_fake_input_func=get_fake_input
                    )
                    logger.info("Loaded model weights from Task Environment and wrapped by NNCF")
                else:
                    try:
                        model.load_state_dict(model_data['model'])
                        logger.info(f"Loaded model weights from Task Environment")
                        logger.info(f"Model architecture: {self._model_name}")
                    except BaseException as ex:
                        raise ValueError("Could not load the saved model. The model file structure is invalid.") \
                            from ex

                logger.info(f"Loaded model weights from Task Environment")
                logger.info(f"Model architecture: {self._model_name}")
            except BaseException as ex:
                raise ValueError("Could not load the saved model. The model file structure is invalid.") \
                    from ex
        else:
            raise ValueError(f"No trained model in project. NNCF require pretrained weights to compress the model")

        self._compression_ctrl = compression_ctrl
        return model


    def _create_compressed_model(self, dataset, config):
        init_dataloader = build_dataloader(
            dataset,
            config.data.samples_per_gpu,
            config.data.workers_per_gpu,
            len(config.gpu_ids),
            dist=False,
            seed=config.seed)
        is_acc_aware_training_set = is_accuracy_aware_training_set(config.get("nncf_config"))

        if is_acc_aware_training_set:
            self._val_dataloader = build_val_dataloader(config, False)

        self._compression_ctrl, self._model = wrap_nncf_model(
            self._model,
            config,
            val_dataloader=self._val_dataloader,
            dataloader_for_init=init_dataloader,
            is_accuracy_aware=is_acc_aware_training_set)

    def optimize(
        self,
        optimization_type: OptimizationType,
        dataset: DatasetEntity,
        output_model: ModelEntity,
        optimization_parameters: Optional[OptimizationParameters],
    ):
        if optimization_type is not OptimizationType.NNCF:
            raise RuntimeError("NNCF is the only supported optimization")

        train_dataset = dataset.get_subset(Subset.TRAINING)
        val_dataset = dataset.get_subset(Subset.VALIDATION)
        config = self._config

        if optimization_parameters is not None:
            update_progress_callback = optimization_parameters.update_progress
        else:
            update_progress_callback = default_progress_callback

        time_monitor = TrainingProgressCallback(update_progress_callback)
        learning_curves = defaultdict(OTELoggerHook.Curve)
        training_config = prepare_for_training(config, train_dataset, val_dataset, time_monitor, learning_curves)

        self._training_work_dir = training_config.work_dir
        mm_train_dataset = build_dataset(training_config.data.train)

        # Initialize NNCF parts if start from not compressed model
        if not self._compression_ctrl:
             self._create_compressed_model(mm_train_dataset, training_config)

        self._is_training = True
        self._model.train()

        train_segmentor(model=self._model,
                        dataset=mm_train_dataset,
                        cfg=training_config,
                        validate=True,
                        compression_ctrl=self._compression_ctrl)

        output_model.model_status = ModelStatus.SUCCESS
        output_model.model_format = ModelFormat.BASE_FRAMEWORK
        output_model.optimization_type = self._optimization_type
        output_model.optimization_methods = self._optimization_methods
        output_model.precision = self._precision

        self._is_training = False

    def export(self, export_type: ExportType, output_model: ModelEntity):
        if self._compression_ctrl is None:
            super().export(export_type, output_model)
        else:
            self._compression_ctrl.prepare_for_export()
            self._model.disable_dynamic_graph_building()
            super().export(export_type, output_model)
            self._model.enable_dynamic_graph_building()

    def save_model(self, output_model: ModelEntity):
        buffer = io.BytesIO()
        hyperparams = self._task_environment.get_hyper_parameters(OTESegmentationConfig)
        hyperparams_str = ids_to_strings(cfg_helper.convert(hyperparams, dict, enum_to_str=True))
        labels = {label.name: label.color.rgb_tuple for label in self._labels}
        modelinfo = {
            'compression_state': self._compression_ctrl.get_compression_state(),
            'meta': {
                'config': self._config,
                'nncf_enable_compression': True,
            },
            'model': self._model.state_dict(),
            'config': hyperparams_str,
            'labels': labels,
            'confidence_threshold': self.confidence_threshold,
            'VERSION': 1,
        }

        torch.save(modelinfo, buffer)
        output_model.set_data("weights.pth", buffer.getvalue())
        output_model.set_data("label_schema.json", label_schema_to_bytes(self._task_environment.label_schema))