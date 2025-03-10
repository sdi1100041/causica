"""
A module to load data from the standard directory structure (i.e. the one followed by csuite)
"""
import json
import logging
import os
from enum import Enum
from functools import partial
from typing import Any, Counter, Optional

import fsspec
import numpy as np
import torch
from tensordict import TensorDict

from causica.datasets.interventional_data import CounterfactualData, InterventionData
from causica.datasets.tensordict_utils import convert_one_hot
from causica.datasets.variable_types import DTYPE_MAP, VariableTypeEnum

CSUITE_DATASETS_PATH = "https://azuastoragepublic.blob.core.windows.net/datasets"


InterventionWithEffects = tuple[InterventionData, InterventionData, set[str]]
CounterfactualWithEffects = tuple[CounterfactualData, Optional[CounterfactualData], set[str]]

logger = logging.getLogger(__name__)


class DataEnum(Enum):
    TRAIN = "train.csv"
    TEST = "test.csv"
    INTERVENTIONS = "interventions.json"
    COUNTERFACTUALS = "counterfactuals.json"
    TRUE_ADJACENCY = "adj_matrix.csv"
    VARIABLES_JSON = "variables.json"


def convert_variable_types_to_enum(variable_metadata: dict[str, Any]):
    # Convert the types to the enum
    for variable in variable_metadata["variables"]:
        variable["type"] = VariableTypeEnum(variable["type"])

    return variable_metadata


def convert_enum_to_variable_types(variable_metadata: dict[str, Any]):
    # Convert enum to types
    for variable in variable_metadata["variables"]:
        variable["type"] = VariableTypeEnum(variable["type"]).value

    return variable_metadata


def load_data(
    root_path: str,
    data_enum: DataEnum,
    variables_metadata: Optional[dict[str, Any]] = None,
    **storage_options: dict[str, Any],
):
    """
    Load the Data from the location, dataset name and type of data.

    Args:
        root_path: The root path to the Data e.g. `CSUITE_DATASETS_PATH/csuite_linexp_2`
        data_enum: The type of dataset for which to return the path
        variables_metadata: Optional variables object (to save downloading it multiple times)
        **storage_options: Keyword args passed to `fsspec.open`
    Return:
        The downloaded data (the type depends on the data requested)
    """

    path_name = os.path.join(root_path, data_enum.value)

    fsspec_open = partial(fsspec.open, mode="r", encoding="utf-8", **storage_options)

    logger.debug(f"Loading {data_enum} from {path_name} with storage options {storage_options}")

    if data_enum == DataEnum.TRUE_ADJACENCY:
        with fsspec_open(path_name) as f:
            return torch.tensor(np.loadtxt(f, dtype=int, delimiter=","))

    if data_enum == DataEnum.VARIABLES_JSON:
        if variables_metadata is not None:
            raise ValueError("Variables metadata was supplied and requested")
        with fsspec_open(path_name) as f:
            return convert_variable_types_to_enum(json.load(f))

    if variables_metadata is None:
        variables_metadata = load_data(root_path, data_enum=DataEnum.VARIABLES_JSON)

    with fsspec_open(path_name) as f:
        if data_enum in {DataEnum.TRAIN, DataEnum.TEST}:
            arr = np.loadtxt(f, delimiter=",")
            categorical_sizes = _get_categorical_sizes(variables_list=variables_metadata["variables"])
            return convert_one_hot(
                tensordict_from_variables_metadata(arr, variables_metadata["variables"]),
                one_hot_sizes=categorical_sizes,
            )
        elif data_enum == DataEnum.INTERVENTIONS:
            return _load_interventions(json_object=json.load(f), metadata=variables_metadata)
        elif data_enum == DataEnum.COUNTERFACTUALS:
            return _load_counterfactuals(json_object=json.load(f), metadata=variables_metadata)
        else:
            raise RuntimeError("Unrecognized data type")


def _load_interventions(json_object: dict[str, Any], metadata: dict[str, Any]) -> list[InterventionWithEffects]:
    """
    Load the Interventional Datasets as a list of interventions/counterfactuals.

    Args:
        json_object: The .json file loaded as a json object.
        metadata: Metadata of the dataset containing names and types.

    Returns:
        A list of interventions and the nodes we want to observe for each
    """
    variables_list = metadata["variables"]

    intervened_column_to_group_name: dict[int, str] = dict(
        zip(
            json_object["metadata"]["columns_to_nodes"],
            list(item["group_name"] for item in variables_list),
        )
    )

    interventions_list = []
    for environment in json_object["environments"]:
        conditioning_idxs = environment["conditioning_idxs"]
        if conditioning_idxs is None:
            condition_nodes = []
        else:
            condition_nodes = [intervened_column_to_group_name[idx] for idx in environment["conditioning_idxs"]]

        intervention_nodes = [intervened_column_to_group_name[idx] for idx in environment["intervention_idxs"]]

        intervention = _to_intervention(
            np.array(environment["test_data"]),
            intervention_nodes=intervention_nodes,
            condition_nodes=condition_nodes,
            variables_list=variables_list,
        )
        # if the json has reference data create another intervention dataclass
        if (reference_data := environment["reference_data"]) is None:
            raise RuntimeError()
        reference = _to_intervention(
            np.array(reference_data),
            intervention_nodes=intervention_nodes,
            condition_nodes=condition_nodes,
            variables_list=variables_list,
        )

        # store the nodes we're interested in observing
        effect_nodes = set(intervened_column_to_group_name[idx] for idx in environment["effect_idxs"])
        # default to all nodes
        if not effect_nodes:
            effect_nodes = set(intervention.sampled_nodes)
        interventions_list.append((intervention, reference, effect_nodes))
    return interventions_list


def _load_counterfactuals(json_object: dict[str, Any], metadata: dict[str, Any]) -> list[CounterfactualWithEffects]:
    """
    Load the Interventional Datasets as a list of counterfactuals.

    Args:
        json_object: The .json file in loaded as a json object.
        metadata: Metadata of the dataset containing names and types.

    Returns:
        A list of counterfactuals and the nodes we want to observe for each
    """
    variables_list = metadata["variables"]

    intervened_column_to_group_name: dict[int, str] = dict(
        zip(
            json_object["metadata"]["columns_to_nodes"],
            list(item["group_name"] for item in variables_list),
        )
    )

    cf_list = []
    for environment in json_object["environments"]:
        factual_data = np.array(environment["conditioning_values"])
        intervention_nodes = [intervened_column_to_group_name[idx] for idx in environment["intervention_idxs"]]
        intervention = _to_counterfactual(
            np.array(environment["test_data"]),
            factual_data,
            intervention_nodes=intervention_nodes,
            variables_list=variables_list,
        )
        # if the json has reference data create another intervention dataclass
        if (reference_data := environment["reference_data"]) is None:
            reference = None
        else:
            reference = _to_counterfactual(
                np.array(reference_data),
                factual_data,
                intervention_nodes=intervention_nodes,
                variables_list=variables_list,
            )

        # store the nodes we're interested in observing
        effect_nodes = set(intervened_column_to_group_name[idx] for idx in environment["effect_idxs"])
        # default to all nodes
        if not effect_nodes:
            effect_nodes = set(intervention.sampled_nodes)
        cf_list.append((intervention, reference, effect_nodes))
    return cf_list


def _to_intervention(
    data: np.ndarray, intervention_nodes: list[str], condition_nodes: list[str], variables_list: list[dict[str, Any]]
) -> InterventionData:
    """Create an `InterventionData` object from the data within the json file."""
    interv_data = tensordict_from_variables_metadata(data, variables_list=variables_list)

    # all the intervention values in the dataset should be the same, so we use the first row
    first_row = interv_data[0]
    assert all(torch.allclose(interv_data[node_name], first_row[node_name]) for node_name in intervention_nodes)
    intervention_values = TensorDict(
        {node_name: first_row[node_name] for node_name in intervention_nodes}, batch_size=tuple()
    )

    condition_values = TensorDict(
        {node_name: first_row[node_name] for node_name in condition_nodes}, batch_size=tuple()
    )

    categorical_sizes = _get_categorical_sizes(variables_list=variables_list)

    return InterventionData(
        intervention_values=convert_one_hot(
            intervention_values, _intersect_dicts_left(categorical_sizes, intervention_values)
        ),
        intervention_data=convert_one_hot(interv_data, _intersect_dicts_left(categorical_sizes, interv_data)),
        condition_values=convert_one_hot(condition_values, _intersect_dicts_left(categorical_sizes, condition_values)),
    )


def _to_counterfactual(
    data: np.ndarray, base_data: np.ndarray, intervention_nodes: list[str], variables_list: list[dict[str, Any]]
) -> CounterfactualData:
    """Create an `CounterfactualData` object from the data within the json file."""
    interv_data = tensordict_from_variables_metadata(data, variables_list=variables_list)
    # all the intervention values in the dataset should be the same, so we use the first row
    first_row = interv_data[0]
    assert all(torch.allclose(interv_data[node_name], first_row[node_name]) for node_name in intervention_nodes)
    intervention_values = TensorDict(
        {node_name: first_row[node_name] for node_name in intervention_nodes}, batch_size=tuple()
    )
    factual_data = tensordict_from_variables_metadata(base_data, variables_list=variables_list)

    categorical_sizes = _get_categorical_sizes(variables_list=variables_list)

    return CounterfactualData(
        intervention_values=convert_one_hot(
            intervention_values, _intersect_dicts_left(categorical_sizes, intervention_values)
        ),
        counterfactual_data=convert_one_hot(interv_data, _intersect_dicts_left(categorical_sizes, interv_data)),
        factual_data=convert_one_hot(factual_data, _intersect_dicts_left(categorical_sizes, factual_data)),
    )


def _get_categorical_sizes(variables_list: list[dict[str, Any]]) -> dict[str, int]:
    categorical_sizes = {}
    for item in variables_list:
        if item["type"] == VariableTypeEnum.CATEGORICAL:
            upper = item.get("upper")
            lower = item.get("lower")
            if upper is not None and lower is not None:
                categorical_sizes[item["group_name"]] = item["upper"] - item["lower"] + 1
            else:
                assert upper is None and lower is None, "Please specify either both limits or neither"
                categorical_sizes[item["group_name"]] = -1
    return categorical_sizes


def tensordict_from_variables_metadata(data: np.ndarray, variables_list: list[dict[str, Any]]) -> TensorDict:
    """Returns a tensor created by concatenating all values along the last dim."""
    assert data.ndim == 2, "Numpy loading only supported for 2d data"
    batch_size = data.shape[0]

    # guaranteed to be ordered correctly in python 3.7+ https://docs.python.org/3/library/collections.html#collections.Counter
    sizes = Counter(d["group_name"] for d in variables_list)  # get the dimensions of each key from the variables
    assert sum(sizes.values()) == data.shape[1], "Variable sizes do not match data shape"

    # NOTE: This assumes that variables in the same group will have the same type.
    dtypes = {item["group_name"]: DTYPE_MAP[item["type"]] for item in variables_list}

    # slice the numpy array and assign the slices to the values of keys in the dictionary
    d = TensorDict({}, batch_size=batch_size)
    curr_idx = 0
    for key, length in sizes.items():
        d[key] = torch.Tensor(data[:, curr_idx : curr_idx + length]).to(dtype=dtypes[key])
        curr_idx += length

    return d


def tensordict_to_tensor(tensor_dict: TensorDict) -> torch.Tensor:
    """
    Convert a `TensorDict` into a 2D `torch.Tensor`.
    """
    return torch.cat(tuple(tensor_dict.values()), dim=-1)


def _intersect_dicts_left(dict_1: dict, dict_2: dict) -> dict:
    """Select the keys that are in both dictionaries, with values from the first."""
    return {key: dict_1[key] for key in dict_1.keys() & dict_2.keys()}
