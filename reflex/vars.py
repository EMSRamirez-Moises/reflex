"""Define a state var."""
from __future__ import annotations

import contextlib
import dataclasses
import dis
import inspect
import json
import random
import re
import string
import sys
from types import CodeType, FunctionType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    _GenericAlias,  # type: ignore
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

import pydantic

from reflex import constants
from reflex.base import Base
from reflex.utils import console, format, imports, serializers, types

# This module used to export ImportVar itself, so we still import it for export here
from reflex.utils.imports import ImportDict, ImportVar

if TYPE_CHECKING:
    from reflex.state import BaseState

# Set of unique variable names.
USED_VARIABLES = set()

# Supported operators for all types.
ALL_OPS = ["==", "!=", "!==", "===", "&&", "||"]
# Delimiters used between function args or operands.
DELIMITERS = [","]
# Mapping of valid operations for different type combinations.
OPERATION_MAPPING = {
    (int, int): {
        "+",
        "-",
        "/",
        "//",
        "*",
        "%",
        "**",
        ">",
        "<",
        "<=",
        ">=",
        "|",
        "&",
    },
    (int, str): {"*"},
    (int, list): {"*"},
    (str, str): {"+", ">", "<", "<=", ">="},
    (float, float): {"+", "-", "/", "//", "*", "%", "**", ">", "<", "<=", ">="},
    (float, int): {"+", "-", "/", "//", "*", "%", "**", ">", "<", "<=", ">="},
    (list, list): {"+", ">", "<", "<=", ">="},
}

# These names were changed in reflex 0.3.0
REPLACED_NAMES = {
    "full_name": "_var_full_name",
    "name": "_var_name",
    "state": "_var_data.state",
    "type_": "_var_type",
    "is_local": "_var_is_local",
    "is_string": "_var_is_string",
    "set_state": "_var_set_state",
    "deps": "_deps",
}


def get_unique_variable_name() -> str:
    """Get a unique variable name.

    Returns:
        The unique variable name.
    """
    name = "".join([random.choice(string.ascii_lowercase) for _ in range(8)])
    if name not in USED_VARIABLES:
        USED_VARIABLES.add(name)
        return name
    return get_unique_variable_name()


class VarData(Base):
    """Metadata associated with a Var."""

    # The name of the enclosing state.
    state: str = ""

    # Imports needed to render this var
    imports: ImportDict = {}

    # Hooks that need to be present in the component to render this var
    hooks: Set[str] = set()

    @classmethod
    def merge(cls, *others: VarData | None) -> VarData | None:
        """Merge multiple var data objects.

        Args:
            *others: The var data objects to merge.

        Returns:
            The merged var data object.
        """
        state = ""
        _imports = {}
        hooks = set()
        for var_data in others:
            if var_data is None:
                continue
            state = state or var_data.state
            _imports = imports.merge_imports(_imports, var_data.imports)
            hooks.update(var_data.hooks)
        return (
            cls(
                state=state,
                imports=_imports,
                hooks=hooks,
            )
            or None
        )

    def __bool__(self) -> bool:
        """Check if the var data is non-empty.

        Returns:
            True if any field is set to a non-default value.
        """
        return bool(self.state or self.imports or self.hooks)

    def __eq__(self, other: Any) -> bool:
        """Check if two var data objects are equal.

        Args:
            other: The other var data object to compare.

        Returns:
            True if all fields are equal and collapsed imports are equal.
        """
        if not isinstance(other, VarData):
            return False
        return (
            self.state == other.state
            and self.hooks == other.hooks
            and imports.collapse_imports(self.imports)
            == imports.collapse_imports(other.imports)
        )

    def dict(self) -> dict:
        """Convert the var data to a dictionary.

        Returns:
            The var data dictionary.
        """
        return {
            "state": self.state,
            "imports": {
                lib: [import_var.dict() for import_var in import_vars]
                for lib, import_vars in self.imports.items()
            },
            "hooks": list(self.hooks),
        }


def _encode_var(value: Var) -> str:
    """Encode the state name into a formatted var.

    Args:
        value: The value to encode the state name into.

    Returns:
        The encoded var.
    """
    if value._var_data:
        return f"<reflex.Var>{value._var_data.json()}</reflex.Var>" + str(value)
    return str(value)


def _decode_var(value: str) -> tuple[VarData | None, str]:
    """Decode the state name from a formatted var.

    Args:
        value: The value to extract the state name from.

    Returns:
        The extracted state name and the value without the state name.
    """
    var_datas = []
    if isinstance(value, str):
        # Extract the state name from a formatted var
        while m := re.match(
            pattern=r"(.*)<reflex.Var>(.*)</reflex.Var>(.*)",
            string=value,
            flags=re.DOTALL,  # Ensure . matches newline characters.
        ):
            value = m.group(1) + m.group(3)
            try:
                var_datas.append(VarData.parse_raw(m.group(2)))
            except pydantic.ValidationError:
                # If the VarData is invalid, it was probably json-encoded twice...
                var_datas.append(VarData.parse_raw(json.loads(f'"{m.group(2)}"')))
    if var_datas:
        return VarData.merge(*var_datas), value
    return None, value


def _extract_var_data(value: Iterable) -> list[VarData | None]:
    """Extract the var imports and hooks from an iterable containing a Var.

    Args:
        value: The iterable to extract the VarData from

    Returns:
        The extracted VarDatas.
    """
    from reflex.style import Style

    var_datas = []
    with contextlib.suppress(TypeError):
        for sub in value:
            if isinstance(sub, Var):
                var_datas.append(sub._var_data)
            elif not isinstance(sub, str):
                # Recurse into dict values.
                if hasattr(sub, "values") and callable(sub.values):
                    var_datas.extend(_extract_var_data(sub.values()))
                # Recurse into iterable values (or dict keys).
                var_datas.extend(_extract_var_data(sub))

    # Style objects should already have _var_data.
    if isinstance(value, Style):
        var_datas.append(value._var_data)
    else:
        # Recurse when value is a dict itself.
        values = getattr(value, "values", None)
        if callable(values):
            var_datas.extend(_extract_var_data(values()))
    return var_datas


class Var:
    """An abstract var."""

    # The name of the var.
    _var_name: str

    # The type of the var.
    _var_type: Type

    # Whether this is a local javascript variable.
    _var_is_local: bool

    # Whether the var is a string literal.
    _var_is_string: bool

    # _var_full_name should be prefixed with _var_state
    _var_full_name_needs_state_prefix: bool

    # Extra metadata associated with the Var
    _var_data: Optional[VarData]

    @classmethod
    def create(
        cls, value: Any, _var_is_local: bool = True, _var_is_string: bool = False
    ) -> Var | None:
        """Create a var from a value.

        Args:
            value: The value to create the var from.
            _var_is_local: Whether the var is local.
            _var_is_string: Whether the var is a string literal.

        Returns:
            The var.

        Raises:
            TypeError: If the value is JSON-unserializable.
        """
        # Check for none values.
        if value is None:
            return None

        # If the value is already a var, do nothing.
        if isinstance(value, Var):
            return value

        # Try to pull the imports and hooks from contained values.
        _var_data = None
        if not isinstance(value, str):
            _var_data = VarData.merge(*_extract_var_data(value))

        # Try to serialize the value.
        type_ = type(value)
        name = value if type_ in types.JSONType else serializers.serialize(value)
        if name is None:
            raise TypeError(
                f"No JSON serializer found for var {value} of type {type_}."
            )
        name = name if isinstance(name, str) else format.json_dumps(name)

        return BaseVar(
            _var_name=name,
            _var_type=type_,
            _var_is_local=_var_is_local,
            _var_is_string=_var_is_string,
            _var_data=_var_data,
        )

    @classmethod
    def create_safe(
        cls, value: Any, _var_is_local: bool = True, _var_is_string: bool = False
    ) -> Var:
        """Create a var from a value, asserting that it is not None.

        Args:
            value: The value to create the var from.
            _var_is_local: Whether the var is local.
            _var_is_string: Whether the var is a string literal.

        Returns:
            The var.
        """
        var = cls.create(
            value,
            _var_is_local=_var_is_local,
            _var_is_string=_var_is_string,
        )
        assert var is not None
        return var

    @classmethod
    def __class_getitem__(cls, type_: str) -> _GenericAlias:
        """Get a typed var.

        Args:
            type_: The type of the var.

        Returns:
            The var class item.
        """
        return _GenericAlias(cls, type_)

    def __post_init__(self) -> None:
        """Post-initialize the var."""
        # Decode any inline Var markup and apply it to the instance
        _var_data, _var_name = _decode_var(self._var_name)
        if _var_data:
            self._var_name = _var_name
            self._var_data = VarData.merge(self._var_data, _var_data)

    def _replace(self, merge_var_data=None, **kwargs: Any) -> Var:
        """Make a copy of this Var with updated fields.

        Args:
            merge_var_data: VarData to merge into the existing VarData.
            **kwargs: Var fields to update.

        Returns:
            A new BaseVar with the updated fields overwriting the corresponding fields in this Var.
        """
        field_values = dict(
            _var_name=kwargs.pop("_var_name", self._var_name),
            _var_type=kwargs.pop("_var_type", self._var_type),
            _var_is_local=kwargs.pop("_var_is_local", self._var_is_local),
            _var_is_string=kwargs.pop("_var_is_string", self._var_is_string),
            _var_full_name_needs_state_prefix=kwargs.pop(
                "_var_full_name_needs_state_prefix",
                self._var_full_name_needs_state_prefix,
            ),
            _var_data=VarData.merge(
                kwargs.get("_var_data", self._var_data), merge_var_data
            ),
        )
        return BaseVar(**field_values)

    def _decode(self) -> Any:
        """Decode Var as a python value.

        Note that Var with state set cannot be decoded python-side and will be
        returned as full_name.

        Returns:
            The decoded value or the Var name.
        """
        if self._var_is_string:
            return self._var_name
        try:
            return json.loads(self._var_name)
        except ValueError:
            return self._var_name

    def equals(self, other: Var) -> bool:
        """Check if two vars are equal.

        Args:
            other: The other var to compare.

        Returns:
            Whether the vars are equal.
        """
        return (
            self._var_name == other._var_name
            and self._var_type == other._var_type
            and self._var_is_local == other._var_is_local
            and self._var_full_name_needs_state_prefix
            == other._var_full_name_needs_state_prefix
            and self._var_data == other._var_data
        )

    def _merge(self, other) -> Var:
        """Merge two or more dicts.

        Args:
            other: The other var to merge.

        Returns:
            The merged var.
        """
        if other is None:
            return self._replace()
        if not isinstance(other, Var):
            other = Var.create(other)
        return self._replace(
            _var_name=f"{{...{self._var_name}, ...{other._var_name}}}"  # type: ignore
        )

    def to_string(self, json: bool = True) -> Var:
        """Convert a var to a string.

        Args:
            json: Whether to convert to a JSON string.

        Returns:
            The stringified var.
        """
        fn = "JSON.stringify" if json else "String"
        return self.operation(fn=fn, type_=str)

    def __hash__(self) -> int:
        """Define a hash function for a var.

        Returns:
            The hash of the var.
        """
        return hash((self._var_name, str(self._var_type)))

    def __str__(self) -> str:
        """Wrap the var so it can be used in templates.

        Returns:
            The wrapped var, i.e. {state.var}.
        """
        out = (
            self._var_full_name
            if self._var_is_local
            else format.wrap(self._var_full_name, "{")
        )
        if self._var_is_string:
            out = format.format_string(out)
        return out

    def __bool__(self) -> bool:
        """Raise exception if using Var in a boolean context.

        Raises:
            TypeError: when attempting to bool-ify the Var.
        """
        raise TypeError(
            f"Cannot convert Var {self._var_full_name!r} to bool for use with `if`, `and`, `or`, and `not`. "
            "Instead use `rx.cond` and bitwise operators `&` (and), `|` (or), `~` (invert)."
        )

    def __iter__(self) -> Any:
        """Raise exception if using Var in an iterable context.

        Raises:
            TypeError: when attempting to iterate over the Var.
        """
        raise TypeError(
            f"Cannot iterate over Var {self._var_full_name!r}. Instead use `rx.foreach`."
        )

    def __format__(self, format_spec: str) -> str:
        """Format the var into a Javascript equivalent to an f-string.

        Args:
            format_spec: The format specifier (Ignored for now).

        Returns:
            The formatted var.
        """
        # Encode the _var_data into the formatted output for tracking purposes.
        str_self = _encode_var(self)
        if self._var_is_local:
            return str_self
        return f"${str_self}"

    def __getitem__(self, i: Any) -> Var:
        """Index into a var.

        Args:
            i: The index to index into.

        Returns:
            The indexed var.

        Raises:
            TypeError: If the var is not indexable.
        """
        # Indexing is only supported for strings, lists, tuples, dicts, and dataframes.
        if not (
            types._issubclass(self._var_type, Union[List, Dict, Tuple, str])
            or types.is_dataframe(self._var_type)
        ):
            if self._var_type == Any:
                raise TypeError(
                    "Could not index into var of type Any. (If you are trying to index into a state var, "
                    "add the correct type annotation to the var.)"
                )
            raise TypeError(
                f"Var {self._var_name} of type {self._var_type} does not support indexing."
            )

        # The type of the indexed var.
        type_ = Any

        # Convert any vars to local vars.
        if isinstance(i, Var):
            i = i._replace(_var_is_local=True)

        # Handle list/tuple/str indexing.
        if types._issubclass(self._var_type, Union[List, Tuple, str]):
            # List/Tuple/String indices must be ints, slices, or vars.
            if (
                not isinstance(i, types.get_args(Union[int, slice, Var]))
                or isinstance(i, Var)
                and not i._var_type == int
            ):
                raise TypeError("Index must be an integer or an integer var.")

            # Handle slices first.
            if isinstance(i, slice):
                # Get the start and stop indices.
                start = i.start or 0
                stop = i.stop or "undefined"

                # Use the slice function.
                return self._replace(
                    _var_name=f"{self._var_name}.slice({start}, {stop})",
                    _var_is_string=False,
                )

            # Get the type of the indexed var.
            if types.is_generic_alias(self._var_type):
                type_ = types.get_args(self._var_type)[0]
            elif types._issubclass(self._var_type, str):
                type_ = str

            # Use `at` to support negative indices.
            return self._replace(
                _var_name=f"{self._var_name}.at({i})",
                _var_type=type_,
                _var_is_string=False,
            )

        # Dictionary / dataframe indexing.
        # Tuples are currently not supported as indexes.
        if (
            (
                types._issubclass(self._var_type, Dict)
                or types.is_dataframe(self._var_type)
            )
            and not isinstance(i, types.get_args(Union[int, str, float, Var]))
        ) or (
            isinstance(i, Var)
            and not types._issubclass(
                i._var_type, types.get_args(Union[int, str, float])
            )
        ):
            raise TypeError(
                "Index must be one of the following types: int, str, int or str Var"
            )
        # Get the type of the indexed var.
        if isinstance(i, str):
            i = format.wrap(i, '"')
        type_ = (
            types.get_args(self._var_type)[1]
            if types.is_generic_alias(self._var_type)
            else Any
        )

        # Use normal indexing here.
        return self._replace(
            _var_name=f"{self._var_name}[{i}]",
            _var_type=type_,
            _var_is_string=False,
        )

    def __getattr__(self, name: str) -> Var:
        """Get a var attribute.

        Args:
            name: The name of the attribute.

        Returns:
            The var attribute.

        Raises:
            AttributeError: If the var is wrongly annotated or can't find attribute.
            TypeError: If an annotation to the var isn't provided.
        """
        # Check if the attribute is one of the class fields.
        if not name.startswith("_"):
            if self._var_type == Any:
                raise TypeError(
                    f"You must provide an annotation for the state var `{self._var_full_name}`. Annotation cannot be `{self._var_type}`"
                ) from None
            is_optional = types.is_optional(self._var_type)
            type_ = types.get_attribute_access_type(self._var_type, name)

            if type_ is not None:
                return self._replace(
                    _var_name=f"{self._var_name}{'?' if is_optional else ''}.{name}",
                    _var_type=type_,
                    _var_is_string=False,
                )

            if name in REPLACED_NAMES:
                raise AttributeError(
                    f"Field {name!r} was renamed to {REPLACED_NAMES[name]!r}"
                )

            raise AttributeError(
                f"The State var `{self._var_full_name}` has no attribute '{name}' or may have been annotated "
                f"wrongly."
            )

        raise AttributeError(
            f"The State var has no attribute '{name}' or may have been annotated wrongly.",
        )

    def operation(
        self,
        op: str = "",
        other: Var | None = None,
        type_: Type | None = None,
        flip: bool = False,
        fn: str | None = None,
        invoke_fn: bool = False,
    ) -> Var:
        """Perform an operation on a var.

        Args:
            op: The operation to perform.
            other: The other var to perform the operation on.
            type_: The type of the operation result.
            flip: Whether to flip the order of the operation.
            fn: A function to apply to the operation.
            invoke_fn: Whether to invoke the function.

        Returns:
            The operation result.

        Raises:
            TypeError: If the operation between two operands is invalid.
            ValueError: If flip is set to true and value of operand is not provided
        """
        if isinstance(other, str):
            other = Var.create(json.dumps(other))
        else:
            other = Var.create(other)

        type_ = type_ or self._var_type

        if other is None and flip:
            raise ValueError(
                "flip_operands cannot be set to True if the value of 'other' operand is not provided"
            )

        left_operand, right_operand = (other, self) if flip else (self, other)

        def get_operand_full_name(operand):
            # operand vars that are string literals need to be wrapped in back ticks.
            return (
                operand._var_name_unwrapped
                if operand._var_is_string
                and not operand._var_state
                and operand._var_is_local
                else operand._var_full_name
            )

        if other is not None:
            # check if the operation between operands is valid.
            if op and not self.is_valid_operation(
                types.get_base_class(left_operand._var_type),  # type: ignore
                types.get_base_class(right_operand._var_type),  # type: ignore
                op,
            ):
                raise TypeError(
                    f"Unsupported Operand type(s) for {op}: `{left_operand._var_full_name}` of type {left_operand._var_type.__name__} and `{right_operand._var_full_name}` of type {right_operand._var_type.__name__}"  # type: ignore
                )

            left_operand_full_name = get_operand_full_name(left_operand)
            right_operand_full_name = get_operand_full_name(right_operand)

            # apply function to operands
            if fn is not None:
                if invoke_fn:
                    # invoke the function on left operand.
                    operation_name = f"{left_operand_full_name}.{fn}({right_operand_full_name})"  # type: ignore
                else:
                    # pass the operands as arguments to the function.
                    operation_name = f"{left_operand_full_name} {op} {right_operand_full_name}"  # type: ignore
                    operation_name = f"{fn}({operation_name})"
            else:
                # apply operator to operands (left operand <operator> right_operand)
                operation_name = f"{left_operand_full_name} {op} {right_operand_full_name}"  # type: ignore
                operation_name = format.wrap(operation_name, "(")
        else:
            # apply operator to left operand (<operator> left_operand)
            operation_name = f"{op}{self._var_full_name}"
            # apply function to operands
            if fn is not None:
                operation_name = (
                    f"{fn}({operation_name})"
                    if not invoke_fn
                    else f"{self._var_full_name}.{fn}()"
                )

        return self._replace(
            _var_name=operation_name,
            _var_type=type_,
            _var_is_string=False,
            _var_full_name_needs_state_prefix=False,
            merge_var_data=other._var_data if other is not None else None,
        )

    @staticmethod
    def is_valid_operation(
        operand1_type: Type, operand2_type: Type, operator: str
    ) -> bool:
        """Check if an operation between two operands is valid.

        Args:
            operand1_type: Type of the operand
            operand2_type: Type of the second operand
            operator: The operator.

        Returns:
            Whether operation is valid or not

        """
        if operator in ALL_OPS or operator in DELIMITERS:
            return True

        # bools are subclasses of ints
        pair = tuple(
            sorted(
                [
                    int if operand1_type == bool else operand1_type,
                    int if operand2_type == bool else operand2_type,
                ],
                key=lambda x: x.__name__,
            )
        )
        return pair in OPERATION_MAPPING and operator in OPERATION_MAPPING[pair]

    def compare(self, op: str, other: Var) -> Var:
        """Compare two vars with inequalities.

        Args:
            op: The comparison operator.
            other: The other var to compare with.

        Returns:
            The comparison result.
        """
        return self.operation(op, other, bool)

    def __invert__(self) -> Var:
        """Invert a var.

        Returns:
            The inverted var.
        """
        return self.operation("!", type_=bool)

    def __neg__(self) -> Var:
        """Negate a var.

        Returns:
            The negated var.
        """
        return self.operation(fn="-")

    def __abs__(self) -> Var:
        """Get the absolute value of a var.

        Returns:
            A var with the absolute value.
        """
        return self.operation(fn="Math.abs")

    def length(self) -> Var:
        """Get the length of a list var.

        Returns:
            A var with the absolute value.

        Raises:
            TypeError: If the var is not a list.
        """
        if not types._issubclass(self._var_type, List):
            raise TypeError(f"Cannot get length of non-list var {self}.")
        return self._replace(
            _var_name=f"{self._var_name}.length",
            _var_type=int,
            _var_is_string=False,
        )

    def __eq__(self, other: Var) -> Var:
        """Perform an equality comparison.

        Args:
            other: The other var to compare with.

        Returns:
            A var representing the equality comparison.
        """
        return self.compare("===", other)

    def __ne__(self, other: Var) -> Var:
        """Perform an inequality comparison.

        Args:
            other: The other var to compare with.

        Returns:
            A var representing the inequality comparison.
        """
        return self.compare("!==", other)

    def __gt__(self, other: Var) -> Var:
        """Perform a greater than comparison.

        Args:
            other: The other var to compare with.

        Returns:
            A var representing the greater than comparison.
        """
        return self.compare(">", other)

    def __ge__(self, other: Var) -> Var:
        """Perform a greater than or equal to comparison.

        Args:
            other: The other var to compare with.

        Returns:
            A var representing the greater than or equal to comparison.
        """
        return self.compare(">=", other)

    def __lt__(self, other: Var) -> Var:
        """Perform a less than comparison.

        Args:
            other: The other var to compare with.

        Returns:
            A var representing the less than comparison.
        """
        return self.compare("<", other)

    def __le__(self, other: Var) -> Var:
        """Perform a less than or equal to comparison.

        Args:
            other: The other var to compare with.

        Returns:
            A var representing the less than or equal to comparison.
        """
        return self.compare("<=", other)

    def __add__(self, other: Var, flip=False) -> Var:
        """Add two vars.

        Args:
            other: The other var to add.
            flip: Whether to flip operands.

        Returns:
            A var representing the sum.
        """
        other_type = other._var_type if isinstance(other, Var) else type(other)
        # For list-list addition, javascript concatenates the content of the lists instead of
        # merging the list, and for that reason we use the spread operator available through spreadArraysOrObjects
        # utility function
        if (
            types.get_base_class(self._var_type) == list
            and types.get_base_class(other_type) == list
        ):
            return self.operation(
                ",", other, fn="spreadArraysOrObjects", flip=flip
            )._replace(
                merge_var_data=VarData(
                    imports={
                        f"/{constants.Dirs.STATE_PATH}": [
                            ImportVar(tag="spreadArraysOrObjects")
                        ]
                    },
                ),
            )
        return self.operation("+", other, flip=flip)

    def __radd__(self, other: Var) -> Var:
        """Add two vars.

        Args:
            other: The other var to add.

        Returns:
            A var representing the sum.
        """
        return self.__add__(other=other, flip=True)

    def __sub__(self, other: Var) -> Var:
        """Subtract two vars.

        Args:
            other: The other var to subtract.

        Returns:
            A var representing the difference.
        """
        return self.operation("-", other)

    def __rsub__(self, other: Var) -> Var:
        """Subtract two vars.

        Args:
            other: The other var to subtract.

        Returns:
            A var representing the difference.
        """
        return self.operation("-", other, flip=True)

    def __mul__(self, other: Var, flip=True) -> Var:
        """Multiply two vars.

        Args:
            other: The other var to multiply.
            flip: Whether to flip operands

        Returns:
            A var representing the product.
        """
        other_type = other._var_type if isinstance(other, Var) else type(other)
        # For str-int multiplication, we use the repeat function.
        # i.e "hello" * 2 is equivalent to "hello".repeat(2) in js.
        if (types.get_base_class(self._var_type), types.get_base_class(other_type)) in [
            (int, str),
            (str, int),
        ]:
            return self.operation(other=other, fn="repeat", invoke_fn=True)

        # For list-int multiplication, we use the Array function.
        # i.e ["hello"] * 2 is equivalent to Array(2).fill().map(() => ["hello"]).flat() in js.
        if (types.get_base_class(self._var_type), types.get_base_class(other_type)) in [
            (int, list),
            (list, int),
        ]:
            other_name = other._var_full_name if isinstance(other, Var) else other
            name = f"Array({other_name}).fill().map(() => {self._var_full_name}).flat()"
            return self._replace(
                _var_name=name,
                _var_type=str,
                _var_is_string=False,
                _var_full_name_needs_state_prefix=False,
            )

        return self.operation("*", other)

    def __rmul__(self, other: Var) -> Var:
        """Multiply two vars.

        Args:
            other: The other var to multiply.

        Returns:
            A var representing the product.
        """
        return self.__mul__(other=other, flip=True)

    def __pow__(self, other: Var) -> Var:
        """Raise a var to a power.

        Args:
            other: The power to raise to.

        Returns:
            A var representing the power.
        """
        return self.operation(",", other, fn="Math.pow")

    def __rpow__(self, other: Var) -> Var:
        """Raise a var to a power.

        Args:
            other: The power to raise to.

        Returns:
            A var representing the power.
        """
        return self.operation(",", other, flip=True, fn="Math.pow")

    def __truediv__(self, other: Var) -> Var:
        """Divide two vars.

        Args:
            other: The other var to divide.

        Returns:
            A var representing the quotient.
        """
        return self.operation("/", other)

    def __rtruediv__(self, other: Var) -> Var:
        """Divide two vars.

        Args:
            other: The other var to divide.

        Returns:
            A var representing the quotient.
        """
        return self.operation("/", other, flip=True)

    def __floordiv__(self, other: Var) -> Var:
        """Divide two vars.

        Args:
            other: The other var to divide.

        Returns:
            A var representing the quotient.
        """
        return self.operation("/", other, fn="Math.floor")

    def __mod__(self, other: Var) -> Var:
        """Get the remainder of two vars.

        Args:
            other: The other var to divide.

        Returns:
            A var representing the remainder.
        """
        return self.operation("%", other)

    def __rmod__(self, other: Var) -> Var:
        """Get the remainder of two vars.

        Args:
            other: The other var to divide.

        Returns:
            A var representing the remainder.
        """
        return self.operation("%", other, flip=True)

    def __and__(self, other: Var) -> Var:
        """Perform a logical and.

        Args:
            other: The other var to perform the logical AND with.

        Returns:
            A var representing the logical AND.

        Note:
            This method provides behavior specific to JavaScript, where it returns the JavaScript
            equivalent code (using the '&&' operator) of a logical AND operation.
            In JavaScript, the
            logical OR operator '&&' is used for Boolean logic, and this method emulates that behavior
            by returning the equivalent code as a Var instance.

            In Python, logical AND 'and' operates differently, evaluating expressions immediately, making
            it challenging to override the behavior entirely.
            Therefore, this method leverages the
            bitwise AND '__and__' operator for custom JavaScript-like behavior.

        Example:
        >>> var1 = Var.create(True)
        >>> var2 = Var.create(False)
        >>> js_code = var1 & var2
        >>> print(js_code._var_full_name)
        '(true && false)'
        """
        return self.operation("&&", other, type_=bool)

    def __rand__(self, other: Var) -> Var:
        """Perform a logical and.

        Args:
            other: The other var to perform the logical AND with.

        Returns:
            A var representing the logical AND.

        Note:
            This method provides behavior specific to JavaScript, where it returns the JavaScript
            equivalent code (using the '&&' operator) of a logical AND operation.
            In JavaScript, the
            logical OR operator '&&' is used for Boolean logic, and this method emulates that behavior
            by returning the equivalent code as a Var instance.

            In Python, logical AND 'and' operates differently, evaluating expressions immediately, making
            it challenging to override the behavior entirely.
            Therefore, this method leverages the
            bitwise AND '__rand__' operator for custom JavaScript-like behavior.

        Example:
        >>> var1 = Var.create(True)
        >>> var2 = Var.create(False)
        >>> js_code = var1 & var2
        >>> print(js_code._var_full_name)
        '(false && true)'
        """
        return self.operation("&&", other, type_=bool, flip=True)

    def __or__(self, other: Var) -> Var:
        """Perform a logical or.

        Args:
            other: The other var to perform the logical or with.

        Returns:
            A var representing the logical or.

        Note:
            This method provides behavior specific to JavaScript, where it returns the JavaScript
            equivalent code (using the '||' operator) of a logical OR operation. In JavaScript, the
            logical OR operator '||' is used for Boolean logic, and this method emulates that behavior
            by returning the equivalent code as a Var instance.

            In Python, logical OR 'or' operates differently, evaluating expressions immediately, making
            it challenging to override the behavior entirely. Therefore, this method leverages the
            bitwise OR '__or__' operator for custom JavaScript-like behavior.

        Example:
        >>> var1 = Var.create(True)
        >>> var2 = Var.create(False)
        >>> js_code = var1 | var2
        >>> print(js_code._var_full_name)
        '(true || false)'
        """
        return self.operation("||", other, type_=bool)

    def __ror__(self, other: Var) -> Var:
        """Perform a logical or.

        Args:
            other: The other var to perform the logical or with.

        Returns:
            A var representing the logical or.

        Note:
            This method provides behavior specific to JavaScript, where it returns the JavaScript
            equivalent code (using the '||' operator) of a logical OR operation. In JavaScript, the
            logical OR operator '||' is used for Boolean logic, and this method emulates that behavior
            by returning the equivalent code as a Var instance.

            In Python, logical OR 'or' operates differently, evaluating expressions immediately, making
            it challenging to override the behavior entirely. Therefore, this method leverages the
            bitwise OR '__or__' operator for custom JavaScript-like behavior.

        Example:
        >>> var1 = Var.create(True)
        >>> var2 = Var.create(False)
        >>> js_code = var1 | var2
        >>> print(js_code)
        'false || true'
        """
        return self.operation("||", other, type_=bool, flip=True)

    def __contains__(self, _: Any) -> Var:
        """Override the 'in' operator to alert the user that it is not supported.

        Raises:
            TypeError: the operation is not supported
        """
        raise TypeError(
            "'in' operator not supported for Var types, use Var.contains() instead."
        )

    def contains(self, other: Any) -> Var:
        """Check if a var contains the object `other`.

        Args:
            other: The object to check.

        Raises:
            TypeError: If the var is not a valid type: dict, list, tuple or str.

        Returns:
            A var representing the contain check.
        """
        if not (types._issubclass(self._var_type, Union[dict, list, tuple, str])):
            raise TypeError(
                f"Var {self._var_full_name} of type {self._var_type} does not support contains check."
            )
        method = (
            "hasOwnProperty"
            if types.get_base_class(self._var_type) == dict
            else "includes"
        )
        if isinstance(other, str):
            other = Var.create(json.dumps(other), _var_is_string=True)
        elif not isinstance(other, Var):
            other = Var.create(other)
        if types._issubclass(self._var_type, Dict):
            return self._replace(
                _var_name=f"{self._var_name}.{method}({other._var_full_name})",
                _var_type=bool,
                _var_is_string=False,
                merge_var_data=other._var_data,
            )
        else:  # str, list, tuple
            # For strings, the left operand must be a string.
            if types._issubclass(self._var_type, str) and not types._issubclass(
                other._var_type, str
            ):
                raise TypeError(
                    f"'in <string>' requires string as left operand, not {other._var_type}"
                )
            return self._replace(
                _var_name=f"{self._var_name}.includes({other._var_full_name})",
                _var_type=bool,
                _var_is_string=False,
                merge_var_data=other._var_data,
            )

    def reverse(self) -> Var:
        """Reverse a list var.

        Raises:
            TypeError: If the var is not a list.

        Returns:
            A var with the reversed list.
        """
        if not types._issubclass(self._var_type, list):
            raise TypeError(f"Cannot reverse non-list var {self._var_full_name}.")

        return self._replace(
            _var_name=f"[...{self._var_full_name}].reverse()",
            _var_is_string=False,
            _var_full_name_needs_state_prefix=False,
        )

    def lower(self) -> Var:
        """Convert a string var to lowercase.

        Returns:
            A var with the lowercase string.

        Raises:
            TypeError: If the var is not a string.
        """
        if not types._issubclass(self._var_type, str):
            raise TypeError(
                f"Cannot convert non-string var {self._var_full_name} to lowercase."
            )

        return self._replace(
            _var_name=f"{self._var_name}.toLowerCase()",
            _var_is_string=False,
            _var_type=str,
        )

    def upper(self) -> Var:
        """Convert a string var to uppercase.

        Returns:
            A var with the uppercase string.

        Raises:
            TypeError: If the var is not a string.
        """
        if not types._issubclass(self._var_type, str):
            raise TypeError(
                f"Cannot convert non-string var {self._var_full_name} to uppercase."
            )

        return self._replace(
            _var_name=f"{self._var_name}.toUpperCase()",
            _var_is_string=False,
            _var_type=str,
        )

    def strip(self, other: str | Var[str] = " ") -> Var:
        """Strip a string var.

        Args:
            other: The string to strip the var with.

        Returns:
            A var with the stripped string.

        Raises:
            TypeError: If the var is not a string.
        """
        if not types._issubclass(self._var_type, str):
            raise TypeError(f"Cannot strip non-string var {self._var_full_name}.")

        other = Var.create_safe(json.dumps(other)) if isinstance(other, str) else other

        return self._replace(
            _var_name=f"{self._var_name}.replace(/^${other._var_full_name}|${other._var_full_name}$/g, '')",
            _var_is_string=False,
            merge_var_data=other._var_data,
        )

    def split(self, other: str | Var[str] = " ") -> Var:
        """Split a string var into a list.

        Args:
            other: The string to split the var with.

        Returns:
            A var with the list.

        Raises:
            TypeError: If the var is not a string.
        """
        if not types._issubclass(self._var_type, str):
            raise TypeError(f"Cannot split non-string var {self._var_full_name}.")

        other = Var.create_safe(json.dumps(other)) if isinstance(other, str) else other

        return self._replace(
            _var_name=f"{self._var_name}.split({other._var_full_name})",
            _var_is_string=False,
            _var_type=list[str],
            merge_var_data=other._var_data,
        )

    def join(self, other: str | Var[str] | None = None) -> Var:
        """Join a list var into a string.

        Args:
            other: The string to join the list with.

        Returns:
            A var with the string.

        Raises:
            TypeError: If the var is not a list.
        """
        if not types._issubclass(self._var_type, list):
            raise TypeError(f"Cannot join non-list var {self._var_full_name}.")

        if other is None:
            other = Var.create_safe('""')
        if isinstance(other, str):
            other = Var.create_safe(json.dumps(other))
        else:
            other = Var.create_safe(other)

        return self._replace(
            _var_name=f"{self._var_name}.join({other._var_full_name})",
            _var_is_string=False,
            _var_type=str,
            merge_var_data=other._var_data,
        )

    def foreach(self, fn: Callable) -> Var:
        """Return a list of components. after doing a foreach on this var.

        Args:
            fn: The function to call on each component.

        Returns:
            A var representing foreach operation.

        Raises:
            TypeError: If the var is not a list.
        """
        inner_types = get_args(self._var_type)
        if not inner_types:
            raise TypeError(
                f"Cannot foreach over non-sequence var {self._var_full_name} of type {self._var_type}."
            )
        arg = BaseVar(
            _var_name=get_unique_variable_name(),
            _var_type=inner_types[0],
        )
        index = BaseVar(
            _var_name=get_unique_variable_name(),
            _var_type=int,
        )
        fn_signature = inspect.signature(fn)
        fn_args = (arg, index)
        fn_ret = fn(*fn_args[: len(fn_signature.parameters)])
        return self._replace(
            _var_name=f"{self._var_full_name}.map(({arg._var_name}, {index._var_name}) => {fn_ret})",
            _var_is_string=False,
        )

    @classmethod
    def range(
        cls,
        v1: Var | int = 0,
        v2: Var | int | None = None,
        step: Var | int | None = None,
    ) -> Var:
        """Return an iterator over indices from v1 to v2 (or 0 to v1).

        Args:
            v1: The start of the range or end of range if v2 is not given.
            v2: The end of the range.
            step: The number of numbers between each item.

        Returns:
            A var representing range operation.

        Raises:
            TypeError: If the var is not an int.
        """
        if not isinstance(v1, Var):
            v1 = Var.create_safe(v1)
        if v1._var_type != int:
            raise TypeError(f"Cannot get range on non-int var {v1._var_full_name}.")
        if not isinstance(v2, Var):
            v2 = Var.create(v2)
        if v2 is None:
            v2 = Var.create_safe("undefined")
        elif v2._var_type != int:
            raise TypeError(f"Cannot get range on non-int var {v2._var_full_name}.")

        if not isinstance(step, Var):
            step = Var.create(step)
        if step is None:
            step = Var.create_safe(1)
        elif step._var_type != int:
            raise TypeError(f"Cannot get range on non-int var {step._var_full_name}.")

        return BaseVar(
            _var_name=f"Array.from(range({v1._var_full_name}, {v2._var_full_name}, {step._var_name}))",
            _var_type=list[int],
            _var_is_local=False,
            _var_data=VarData.merge(
                v1._var_data,
                v2._var_data,
                step._var_data,
                VarData(
                    imports={
                        "/utils/helpers/range.js": [
                            ImportVar(tag="range", is_default=True),
                        ],
                    },
                ),
            ),
        )

    def to(self, type_: Type) -> Var:
        """Convert the type of the var.

        Args:
            type_: The type to convert to.

        Returns:
            The converted var.
        """
        return self._replace(_var_type=type_)

    def as_ref(self) -> Var:
        """Convert the var to a ref.

        Returns:
            The var as a ref.
        """
        return self._replace(
            _var_name=f"refs['{self._var_full_name}']",
            _var_is_local=True,
            _var_is_string=False,
            _var_full_name_needs_state_prefix=False,
            merge_var_data=VarData(
                imports={
                    f"/{constants.Dirs.STATE_PATH}": [imports.ImportVar(tag="refs")],
                },
            ),
        )

    @property
    def _var_full_name(self) -> str:
        """Get the full name of the var.

        Returns:
            The full name of the var.
        """
        if not self._var_full_name_needs_state_prefix:
            return self._var_name
        return (
            self._var_name
            if self._var_data is None or self._var_data.state == ""
            else ".".join(
                [format.format_state_name(self._var_data.state), self._var_name]
            )
        )

    def _var_set_state(self, state: Type[BaseState] | str) -> Any:
        """Set the state of the var.

        Args:
            state: The state to set or the full name of the state.

        Returns:
            The var with the set state.
        """
        state_name = state if isinstance(state, str) else state.get_full_name()
        new_var_data = VarData(
            state=state_name,
            hooks={
                "const {0} = useContext(StateContexts.{0})".format(
                    format.format_state_name(state_name)
                )
            },
            imports={
                f"/{constants.Dirs.CONTEXTS_PATH}": [ImportVar(tag="StateContexts")],
                "react": [ImportVar(tag="useContext")],
            },
        )
        self._var_data = VarData.merge(self._var_data, new_var_data)
        self._var_full_name_needs_state_prefix = True
        return self

    @property
    def _var_state(self) -> str:
        """Compat method for getting the state.

        Returns:
            The state name associated with the var.
        """
        return self._var_data.state if self._var_data else ""

    @property
    def _var_name_unwrapped(self) -> str:
        """Get the var str without wrapping in curly braces.

        Returns:
            The str var without the wrapped curly braces
        """
        from reflex.style import Style

        type_ = (
            get_origin(self._var_type)
            if types.is_generic_alias(self._var_type)
            else self._var_type
        )

        wrapped_var = str(self)
        return (
            wrapped_var
            if not self._var_state
            and issubclass(type_, dict)
            or issubclass(type_, Style)
            else wrapped_var.strip("{}")
        )


# Allow automatic serialization of Var within JSON structures
serializers.serializer(_encode_var)


@dataclasses.dataclass(
    eq=False,
    **{"slots": True} if sys.version_info >= (3, 10) else {},
)
class BaseVar(Var):
    """A base (non-computed) var of the app state."""

    # The name of the var.
    _var_name: str = dataclasses.field()

    # The type of the var.
    _var_type: Type = dataclasses.field(default=Any)

    # Whether this is a local javascript variable.
    _var_is_local: bool = dataclasses.field(default=False)

    # Whether the var is a string literal.
    _var_is_string: bool = dataclasses.field(default=False)

    # _var_full_name should be prefixed with _var_state
    _var_full_name_needs_state_prefix: bool = dataclasses.field(default=False)

    # Extra metadata associated with the Var
    _var_data: Optional[VarData] = dataclasses.field(default=None)

    def __hash__(self) -> int:
        """Define a hash function for a var.

        Returns:
            The hash of the var.
        """
        return hash((self._var_name, str(self._var_type)))

    def get_default_value(self) -> Any:
        """Get the default value of the var.

        Returns:
            The default value of the var.

        Raises:
            ImportError: If the var is a dataframe and pandas is not installed.
        """
        if types.is_optional(self._var_type):
            return None

        type_ = (
            get_origin(self._var_type)
            if types.is_generic_alias(self._var_type)
            else self._var_type
        )
        if type_ is Literal:
            args = get_args(self._var_type)
            return args[0] if args else None
        if issubclass(type_, str):
            return ""
        if issubclass(type_, types.get_args(Union[int, float])):
            return 0
        if issubclass(type_, bool):
            return False
        if issubclass(type_, list):
            return []
        if issubclass(type_, dict):
            return {}
        if issubclass(type_, tuple):
            return ()
        if types.is_dataframe(type_):
            try:
                import pandas as pd

                return pd.DataFrame()
            except ImportError as e:
                raise ImportError(
                    "Please install pandas to use dataframes in your app."
                ) from e
        return set() if issubclass(type_, set) else None

    def get_setter_name(self, include_state: bool = True) -> str:
        """Get the name of the var's generated setter function.

        Args:
            include_state: Whether to include the state name in the setter name.

        Returns:
            The name of the setter function.
        """
        setter = constants.SETTER_PREFIX + self._var_name
        if self._var_data is None:
            return setter
        if not include_state or self._var_data.state == "":
            return setter
        return ".".join((self._var_data.state, setter))

    def get_setter(self) -> Callable[[BaseState, Any], None]:
        """Get the var's setter function.

        Returns:
            A function that that creates a setter for the var.
        """

        def setter(state: BaseState, value: Any):
            """Get the setter for the var.

            Args:
                state: The state within which we add the setter function.
                value: The value to set.
            """
            if self._var_type in [int, float]:
                try:
                    value = self._var_type(value)
                    setattr(state, self._var_name, value)
                except ValueError:
                    console.warn(
                        f"{self._var_name}: Failed conversion of {value} to '{self._var_type.__name__}'. Value not set.",
                    )
            else:
                setattr(state, self._var_name, value)

        setter.__qualname__ = self.get_setter_name()

        return setter


@dataclasses.dataclass(init=False, eq=False)
class ComputedVar(Var, property):
    """A field with computed getters."""

    # Whether to track dependencies and cache computed values
    _cache: bool = dataclasses.field(default=False)

    def __init__(
        self,
        fget: Callable[[BaseState], Any],
        fset: Callable[[BaseState, Any], None] | None = None,
        fdel: Callable[[BaseState], Any] | None = None,
        doc: str | None = None,
        **kwargs,
    ):
        """Initialize a ComputedVar.

        Args:
            fget: The getter function.
            fset: The setter function.
            fdel: The deleter function.
            doc: The docstring.
            **kwargs: additional attributes to set on the instance
        """
        property.__init__(self, fget, fset, fdel, doc)
        kwargs["_var_name"] = kwargs.pop("_var_name", fget.__name__)
        kwargs["_var_type"] = kwargs.pop("_var_type", self._determine_var_type())
        BaseVar.__init__(self, **kwargs)  # type: ignore

    @property
    def _cache_attr(self) -> str:
        """Get the attribute used to cache the value on the instance.

        Returns:
            An attribute name.
        """
        return f"__cached_{self._var_name}"

    def __get__(self, instance, owner):
        """Get the ComputedVar value.

        If the value is already cached on the instance, return the cached value.

        Args:
            instance: the instance of the class accessing this computed var.
            owner: the class that this descriptor is attached to.

        Returns:
            The value of the var for the given instance.
        """
        if instance is None or not self._cache:
            return super().__get__(instance, owner)

        # handle caching
        if not hasattr(instance, self._cache_attr):
            setattr(instance, self._cache_attr, super().__get__(instance, owner))
        return getattr(instance, self._cache_attr)

    def _deps(
        self,
        objclass: Type,
        obj: FunctionType | CodeType | None = None,
        self_name: Optional[str] = None,
    ) -> set[str]:
        """Determine var dependencies of this ComputedVar.

        Save references to attributes accessed on "self".  Recursively called
        when the function makes a method call on "self" or define comprehensions
        or nested functions that may reference "self".

        Args:
            objclass: the class obj this ComputedVar is attached to.
            obj: the object to disassemble (defaults to the fget function).
            self_name: if specified, look for this name in LOAD_FAST and LOAD_DEREF instructions.

        Returns:
            A set of variable names accessed by the given obj.
        """
        d = set()
        if obj is None:
            if self.fget is not None:
                obj = cast(FunctionType, self.fget)
            else:
                return set()
        with contextlib.suppress(AttributeError):
            # unbox functools.partial
            obj = cast(FunctionType, obj.func)  # type: ignore
        with contextlib.suppress(AttributeError):
            # unbox EventHandler
            obj = cast(FunctionType, obj.fn)  # type: ignore

        if self_name is None and isinstance(obj, FunctionType):
            try:
                # the first argument to the function is the name of "self" arg
                self_name = obj.__code__.co_varnames[0]
            except (AttributeError, IndexError):
                self_name = None
        if self_name is None:
            # cannot reference attributes on self if method takes no args
            return set()
        self_is_top_of_stack = False
        for instruction in dis.get_instructions(obj):
            if (
                instruction.opname in ("LOAD_FAST", "LOAD_DEREF")
                and instruction.argval == self_name
            ):
                # bytecode loaded the class instance to the top of stack, next load instruction
                # is referencing an attribute on self
                self_is_top_of_stack = True
                continue
            if self_is_top_of_stack and instruction.opname in (
                "LOAD_ATTR",
                "LOAD_METHOD",
            ):
                try:
                    ref_obj = getattr(objclass, instruction.argval)
                except Exception:
                    ref_obj = None
                if callable(ref_obj):
                    # recurse into callable attributes
                    d.update(
                        self._deps(
                            objclass=objclass,
                            obj=ref_obj,
                        )
                    )
                else:
                    # normal attribute access
                    d.add(instruction.argval)
            elif instruction.opname == "LOAD_CONST" and isinstance(
                instruction.argval, CodeType
            ):
                # recurse into nested functions / comprehensions, which can reference
                # instance attributes from the outer scope
                d.update(
                    self._deps(
                        objclass=objclass,
                        obj=instruction.argval,
                        self_name=self_name,
                    )
                )
            self_is_top_of_stack = False
        return d

    def mark_dirty(self, instance) -> None:
        """Mark this ComputedVar as dirty.

        Args:
            instance: the state instance that needs to recompute the value.
        """
        with contextlib.suppress(AttributeError):
            delattr(instance, self._cache_attr)

    def _determine_var_type(self) -> Type:
        """Get the type of the var.

        Returns:
            The type of the var.
        """
        hints = get_type_hints(self.fget)
        if "return" in hints:
            return hints["return"]
        return Any


def cached_var(fget: Callable[[Any], Any]) -> ComputedVar:
    """A field with computed getter that tracks other state dependencies.

    The cached_var will only be recalculated when other state vars that it
    depends on are modified.

    Args:
        fget: the function that calculates the variable value.

    Returns:
        ComputedVar that is recomputed when dependencies change.
    """
    cvar = ComputedVar(fget=fget)
    cvar._cache = True
    return cvar


class CallableVar(BaseVar):
    """Decorate a Var-returning function to act as both a Var and a function.

    This is used as a compatibility shim for replacing Var objects in the
    API with functions that return a family of Var.
    """

    def __init__(self, fn: Callable[..., BaseVar]):
        """Initialize a CallableVar.

        Args:
            fn: The function to decorate (must return Var)
        """
        self.fn = fn
        default_var = fn()
        super().__init__(**dataclasses.asdict(default_var))

    def __call__(self, *args, **kwargs) -> BaseVar:
        """Call the decorated function.

        Args:
            *args: The args to pass to the function.
            **kwargs: The kwargs to pass to the function.

        Returns:
            The Var returned from calling the function.
        """
        return self.fn(*args, **kwargs)
