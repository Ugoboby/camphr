"""Module trf_model defines pytorch-transformers components."""
import dataclasses
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np
import spacy
import spacy.language
import torch
from camphr.pipelines.trf_auto import get_trf_model_cls
from camphr.pipelines.trf_utils import ATTRS, TrfAutoMixin
from camphr.pipelines.utils import get_similarity
from camphr.torch_utils import (
    OptimizerParameters,
    TensorWrapper,
    TorchPipe,
    get_parameters_with_decay,
)
from camphr.utils import zero_pad
from spacy.gold import GoldParse
from spacy.tokens import Doc

spacy.language.ENABLE_PIPELINE_ANALYSIS = True


@dataclasses.dataclass
class TransformersModelInputs:
    input_ids: torch.Tensor
    token_type_ids: Optional[torch.Tensor] = None
    attention_mask: Optional[torch.Tensor] = None


TRANSFORMERS_MODEL = "transformers_model"


@spacy.component(TRANSFORMERS_MODEL, assigns=[f"doc._.{ATTRS.last_hidden_state}"])
class TransformersModel(TrfAutoMixin, TorchPipe):
    """Transformers Model component."""

    _TRF_NAME = "trf_name"
    _MODEL_CLS_GETTER = get_trf_model_cls

    @property
    def max_length(self) -> int:
        return self.model.config.max_position_embeddings

    def _get_last_hidden_state(self, output: Tuple[Any]) -> torch.Tensor:
        # assumes output[0] is the last hidden state
        return output[0]

    def predict(self, docs: List[Doc]) -> torch.Tensor:
        self.require_model()
        self.model.eval()
        x = self._docs_to_trfinput(docs)
        with torch.no_grad():
            y = self.model(**dataclasses.asdict(x))
        return self._get_last_hidden_state(y)

    def set_annotations(
        self, docs: List[Doc], outputs: torch.Tensor, set_vector: bool = True
    ) -> None:
        """Assign the extracted features to the Doc.

        Args:
            set_vector: If True, attach the vector to doc. This may harms the performance.
        """
        for i, doc in enumerate(docs):
            length = len(doc._.get(ATTRS.token_ids))
            # Instead of assigning tensor directory, assign `TensorWrapper`
            # so that trailing pipe can handle batch tensor efficiently.
            doc._.set(ATTRS.last_hidden_state, TensorWrapper(outputs, i, length))

            if set_vector:
                lh: torch.Tensor = doc._.get(ATTRS.last_hidden_state).get()
                doc_tensor = lh.new_zeros((len(doc), lh.shape[-1]))
                # TODO: Inefficient
                # TODO: Store the functionality into user_hooks after https://github.com/explosion/spaCy/issues/4439 was released
                for i, a in enumerate(doc._.get(ATTRS.align)):
                    if self.max_length > 0:
                        a = [aa for aa in a if aa < len(lh)]
                    doc_tensor[i] += lh[a].sum(0)
                doc.tensor = doc_tensor
                doc.user_hooks["vector"] = get_doc_vector_via_tensor
                doc.user_span_hooks["vector"] = get_span_vector_via_tensor
                doc.user_token_hooks["vector"] = get_token_vector_via_tensor
                doc.user_hooks["similarity"] = get_similarity
                doc.user_span_hooks["similarity"] = get_similarity
                doc.user_token_hooks["similarity"] = get_similarity

    @property
    def freeze(self) -> bool:
        if self.cfg.get("freeze"):
            return True
        return False

    def update(self, docs: List[Doc], golds: List[GoldParse]):
        """Simply forward `docs` in training mode."""
        self.require_model()
        if self.freeze:
            torch.set_grad_enabled(False)
            self.model.eval()
        else:
            self.model.train()
        y = self.predict(docs)
        torch.set_grad_enabled(True)

        # `set_vector = False` because the tensor may not be necessary in updating.
        # The tensor is still available via doc._.transformers_last_hidden_state.
        self.set_annotations(docs, y, set_vector=False)

    def _docs_to_trfinput(self, docs: Sequence[Doc]) -> TransformersModelInputs:
        """Generate input data for transformers model from docs."""
        token_ids_list = [doc._.get(ATTRS.token_ids) for doc in docs]
        inputs = TransformersModelInputs(
            input_ids=torch.tensor(zero_pad(token_ids_list), device=self.device)
        )
        inputs.attention_mask = torch.tensor(
            zero_pad([doc._.get(ATTRS.attention_mask) for doc in docs]),
            device=self.device,
        )
        inputs.token_type_ids = torch.tensor(
            zero_pad([doc._.get(ATTRS.attention_mask) for doc in docs]),
            device=self.device,
        )
        return inputs

    def optim_parameters(self) -> OptimizerParameters:
        if self.freeze:
            return []
        no_decay = self.cfg.get("no_decay")
        weight_decay = self.cfg.get("weight_decay")
        return get_parameters_with_decay(self.model, no_decay, weight_decay)


def get_doc_vector_via_tensor(doc) -> np.ndarray:
    return doc.tensor.sum(0).cpu().numpy()


def get_span_vector_via_tensor(span) -> np.ndarray:
    return span.doc.tensor[span.start : span.end].sum(0).cpu().numpy()


def get_token_vector_via_tensor(token) -> np.ndarray:
    return token.doc.tensor[token.i].cpu().numpy()