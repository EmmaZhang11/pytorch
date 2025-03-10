# mypy: ignore-errors

import functools

import inspect
import operator
import types
from typing import Dict, List


try:
    import numpy as np
except ModuleNotFoundError:
    np = None


import sympy

import torch._numpy as tnp

import torch.fx
import torch.random
from torch._dynamo import compiled_autograd

from torch.fx.experimental.symbolic_shapes import (
    guard_scalar,
    GuardOnDataDependentSymNode,
    has_free_symbols,
    is_symbolic,
    SymTypes,
)

from .. import config, variables
from .._trace_wrapped_higher_order_op import trace_wrapped

from ..exc import unimplemented, UserError, UserErrorType
from ..guards import GuardBuilder, install_guard
from ..source import AttrSource
from ..utils import (
    fqn,
    get_custom_getattr,
    get_fake_value,
    get_real_value,
    guard_if_dyn,
    object_has_getattribute,
    product,
    proxy_args_kwargs,
    tensortype_to_dtype,
)
from .base import VariableTracker
from .constant import ConstantVariable
from .lists import SizeVariable

supported_tensor_comparison_ops = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}
supported_const_comparison_ops = {
    "is": operator.is_,
    "is not": operator.is_not,
    "==": operator.eq,
    "!=": operator.ne,
}


class TensorVariable(VariableTracker):
    """A torch.Tensor input or an intermediate value in the FX graph"""

    _nonvar_fields = {
        "proxy",
        "dtype",
        "device",
        "layout",
        "ndim",
        "size",
        "stride",
        "requires_grad",
        "is_quantized",
        "is_contiguous",
        "is_sparse",
        "class_type",
        "specialized_value",
        *VariableTracker._nonvar_fields,
    }

    def get_real_value(self):
        """
        Get the actual value represented by this variable if computation is run
        using the user-provided inputs.
        NOTE: this runs actual tensor computation and may be
        slow and memory-intensive.
        """
        return get_real_value(self.proxy.node, self.proxy.tracer)

    def __init__(
        self,
        proxy: torch.fx.Proxy,
        *,
        dtype,
        device,
        layout,
        ndim,
        requires_grad,
        is_quantized,
        is_sparse,
        class_type,
        size=None,
        stride=None,
        is_contiguous=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.proxy = proxy
        self.dtype = dtype
        self.device = device
        self.layout = layout
        self.ndim = ndim
        self.size = size
        self.stride = stride
        self.requires_grad = requires_grad
        self.is_quantized = is_quantized
        self.is_contiguous = is_contiguous
        self.is_sparse = is_sparse
        self.class_type = class_type

    def as_proxy(self):
        return self.proxy

    def python_type(self):
        return self.class_type

    @staticmethod
    def specialize(value: torch.Tensor):
        props = {
            "dtype": value.dtype,
            "device": value.device,
            "layout": value.layout,
            "ndim": int(value.ndim),
            "requires_grad": value.requires_grad,
            "is_quantized": value.is_quantized,
            "is_sparse": value.is_sparse,
            "class_type": type(value),
        }
        if not has_free_symbols(value):
            # this is a fully static shape, and the keys on props here inform specialization.
            # We have to cast to int here, because these might get accessed as ConstantVariable, which has
            # a strict no-symint policy. If we got here due to not having free symbols, this is a known constant
            # already. We could remove the discrepancy here, by having ConstantVariable be more permissive for
            # constant backed SymInts, but that assert being strict has led to some good signal in hunting bugs, and
            # I'd like to keep it around for now.
            props["size"] = tuple(
                # the non is_symbolic case applies to the jagged layout
                # NestedTensor case as singleton ints are not symbolic
                [int(s) if is_symbolic(s) else s for s in value.size()]
            )
            props["stride"] = tuple(value.stride())
            if torch._C._functorch.is_batchedtensor(value):
                # Batched tensors does not support contiguity patterns, so
                # we refrain from computing the `is_contiguous` property
                props["is_contiguous"] = None
            else:
                props["is_contiguous"] = tuple(
                    [
                        x
                        for x in torch._prims_common._memory_formats
                        if value.is_contiguous(memory_format=x)
                    ]
                )
        return props

    def dynamic_getattr(self, tx, name):
        if not self.source:
            raise NotImplementedError()

        # For local source, we associate the real value. We use this real value
        # for implementing getattr fallthrough on the variable tracker base class.

        # Note - this scope construction is mirrored in guards
        # A subsequent PR will introduce a util.
        scope = {"L": tx.output.local_scope, "G": tx.output.global_scope}
        try:
            # We raise in case we get a typerror bug w/ SuperSource.
            # SuperSource has bugs in it atm, and can produce code like
            # eval("super(L['mod'].model.model.encoder.embed_positions.forward__class__,
            # L['mod'].model.model.encoder.embed_positions)", scope)
            # Which is incorrect, and violates the invariant that all sources should be eval()-able against the scope.
            _input_associated_real_value = eval(self.source.name(), scope)
        except Exception as exc:
            raise NotImplementedError() from exc

        if _input_associated_real_value is None:
            raise NotImplementedError()

        if object_has_getattribute(_input_associated_real_value):
            raise NotImplementedError()

        if get_custom_getattr(_input_associated_real_value):
            raise NotImplementedError()

        real_value = getattr(_input_associated_real_value, name)
        if callable(real_value):
            # Callables have more nuanced handling, and we should let the existing system delegate here.
            # Raising was past behavior and so should always be sound to fall back.
            # Note - at a certain point we may want to handle
            raise NotImplementedError()

        from ..guards import GuardBuilder
        from .builder import VariableBuilder

        attr_source = AttrSource(self.source, name)
        install_guard(attr_source.make_guard(GuardBuilder.HASATTR))
        return VariableBuilder(tx, attr_source)(real_value)

    def var_getattr(self, tx, name):
        from . import ConstantVariable, UserDefinedClassVariable

        if tx.strict_checks_enabled:
            if name in self._strict_mode_banned_ops():
                unimplemented(f"Illegal getattr invocation {name} in strict mode")

        result = None
        if name == "ndim" and self.ndim is not None:
            result = ConstantVariable.create(self.ndim)
        elif name == "dtype" and self.dtype is not None:
            result = ConstantVariable.create(self.dtype)
        elif name == "device" and self.device is not None:
            result = ConstantVariable.create(self.device)
        elif name == "layout" and self.layout is not None:
            result = ConstantVariable.create(self.layout)
        elif name == "is_cuda" and self.device is not None:
            result = ConstantVariable.create(self.device.type == "cuda")
        elif name == "shape" and self.size is not None:
            sizes = [variables.ConstantVariable.create(x) for x in self.size]
            result = SizeVariable(sizes)
        elif name == "requires_grad" and self.requires_grad is not None:
            result = ConstantVariable.create(self.requires_grad)
        elif name == "is_quantized" and self.is_quantized is not None:
            result = ConstantVariable.create(self.is_quantized)
        elif name == "is_sparse" and self.is_sparse is not None:
            result = ConstantVariable.create(self.is_sparse)
        elif name == "shape" and self.size is None:
            result = self.call_method(tx, "size", [], {})
        elif name == "ndim" and self.ndim is None:
            result = self.call_method(tx, "dim", [], {})
        elif name == "data":
            result = self.call_method(tx, "detach", [], {})
        if name == "__class__":
            return UserDefinedClassVariable(self.python_type())

        # Add a guard for type matching, these guards are checked before tensor guards
        # In some cases, a <tensor>.<attr> guard can be evaluated first, and break if
        # <tensor> is later changed to another type
        if result is not None and self.source is not None:
            install_guard(self.make_guard(GuardBuilder.TYPE_MATCH))
            result.source = AttrSource(self.source, name)

        # It's hard to get inplace view (metadata mutation) on graph input work properly across
        # dynamo/aot/inductor, just fall back.
        if self.source is not None and hasattr(torch.ops.aten, name):
            fn = getattr(torch.ops.aten, name)
            if (
                hasattr(fn, "overloads")
                and hasattr(fn, fn.overloads()[0])
                and torch.Tag.inplace_view in getattr(fn, fn.overloads()[0]).tags
            ):
                # Delay the graph break to the actual call of unsqueeze_/resize_/resize_as_ etc.
                return variables.misc.DelayGraphBreakVariable(
                    source=AttrSource(self.source, name)
                )

        # For attributes (not methods) that were not caught in the special handling above,
        # (e.g. tensor.real), we handle these generically, assuming that the output type is
        # a tensor.
        if result is None:

            def try_generic_attr_handling():
                from .builder import wrap_fx_proxy
                from .misc import GetAttrVariable

                try:
                    static_attr = inspect.getattr_static(torch.Tensor, name)
                except AttributeError:
                    return None

                # Make sure this is an attribute, not a method.
                # type(torch.Tensor.H) should be "getset_descriptor"
                # This is a because of CPython implementation, see THPVariableType:
                # these attributes are implemented under tp_getset, which appear
                # as `getset_descriptor`s, (compared to, say, methods which appear
                # as `method_descriptor`s)
                if type(static_attr) != types.GetSetDescriptorType:
                    return None

                proxy = GetAttrVariable.create_getattr_proxy(self.as_proxy(), name)
                if self.source is not None:
                    return wrap_fx_proxy(
                        tx=tx, proxy=proxy, source=AttrSource(self.source, name)
                    )
                else:
                    return wrap_fx_proxy(tx=tx, proxy=proxy)

            result = try_generic_attr_handling()

        if result is None:
            result = self.dynamic_getattr(tx, name)

        if result is None:
            raise NotImplementedError()
        return result

    def has_unpack_var_sequence(self, tx):
        return self.ndim > 0

    def unpack_var_sequence(self, tx, idxes=None):
        from .builder import wrap_fx_proxy_cls

        if idxes is None:
            if self.size:
                length = self.size[0]
            else:
                dyn_length = self.call_method(
                    tx, "size", [ConstantVariable.create(0)], {}
                )
                # SymNodeVariable for symbolic sizes, ConstantVariable for constants OR values produced through
                # symbolic_shapes, but that end up as int/sympy.Integer
                assert isinstance(dyn_length, (SymNodeVariable, ConstantVariable))
                if isinstance(dyn_length, SymNodeVariable):
                    length = dyn_length.evaluate_expr(tx.output)
                else:
                    length = dyn_length.value
            idxes = range(length)
        return [
            wrap_fx_proxy_cls(target_cls=type(self), tx=tx, proxy=self.as_proxy()[i])
            for i in idxes
        ]

    def _strict_mode_banned_ops(self):
        return torch._dynamo.config._autograd_backward_strict_mode_banned_ops

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        if tx.strict_checks_enabled:
            if name in self._strict_mode_banned_ops():
                unimplemented(f"Illegal method invocation {name} in strict mode")

        """
        Dispatch to a method-specific handler defined below.  If the
        handler returns None (or doesn't exist) we put the method call
        in the graph.
        """
        try:
            handler_method = getattr(self, f"method_{name}")
        except AttributeError:
            pass
        else:
            try:
                result = handler_method(*args, **kwargs)
                if result:
                    return result
            except TypeError as e:
                unimplemented(f"unhandled args for {name}: {e}")

        from .builder import wrap_fx_proxy

        return wrap_fx_proxy(
            tx,
            tx.output.create_proxy(
                "call_method",
                name,
                *proxy_args_kwargs([self, *args], kwargs),
            ),
        )

    def method_size(self, *args, **kwargs):
        return self._method_size_stride("size", *args, **kwargs)

    def method_stride(self, *args, **kwargs):
        return self._method_size_stride("stride", *args, **kwargs)

    def _method_size_stride(self, name, dim=None):
        dim = guard_if_dyn(dim)

        def make_const_size_variable(x, **options):
            return SizeVariable(
                [ConstantVariable.create(y, **options) for y in x], **options
            )

        RetVariable = (
            make_const_size_variable if name == "size" else ConstantVariable.create
        )

        # Technically, this should not be necessary, but I'm including it
        # for enhanced BC, in case example_value is sometimes not set
        # (it really should always be set though!)
        if (r := getattr(self, name)) is not None:
            if dim is None:
                return RetVariable(r)
            else:
                return ConstantVariable.create(r[dim])

        # It might still be constant!  Consult the fake tensor and see
        if (fake := self.proxy.node.meta.get("example_value")) is not None:
            if dim is None:
                fake_r = getattr(fake, name)()
                if not has_free_symbols(fake_r):
                    # int conversion for safety, in case a SymInt refined
                    # to constant
                    return RetVariable(tuple(int(r) for r in fake_r))
            else:
                fake_r = getattr(fake, name)(dim)
                if not has_free_symbols(fake_r):
                    return ConstantVariable.create(int(fake_r))

    def method_numel(self):
        if self.size is not None:
            return ConstantVariable.create(product(self.size))

        # It might still be constant!  Consult the fake tensor and see
        if (fake := self.proxy.node.meta.get("example_value")) is not None:
            fake_r = fake.numel()
            if not has_free_symbols(fake_r):
                return ConstantVariable.create(int(fake_r))

    method_nelement = method_numel

    def method_dim(self):
        if self.ndim is not None:
            return ConstantVariable.create(self.ndim)

    method_ndimension = method_dim

    def method_is_floating_point(self):
        if self.dtype is not None:
            return ConstantVariable.create(self.dtype.is_floating_point)

    def method_is_contiguous(self, memory_format=None):
        memory_format = (
            memory_format.as_python_constant()
            if memory_format is not None
            else torch.contiguous_format
        )
        if self.is_contiguous is not None:
            return ConstantVariable.create(memory_format in self.is_contiguous)
        elif (fake := self.proxy.node.meta.get("example_value")) is not None:
            return ConstantVariable.create(
                fake.is_contiguous(memory_format=memory_format)
            )

    def method_type(self, dtype=None, non_blocking=False, **kwargs):
        if (
            dtype is None
            and self.dtype is not None
            and isinstance(self.device, torch.device)
        ):
            tensortype = next(
                k for k, v in tensortype_to_dtype.items() if self.dtype in v
            )
            if self.device.type == "cuda":
                return ConstantVariable.create(f"torch.cuda.{tensortype.__name__}")
            else:
                return ConstantVariable.create(f"torch.{tensortype.__name__}")
        elif (
            dtype is not None
            and fqn(type(dtype.as_python_constant())) == "torch.tensortype"
        ):
            # torch.FloatTensor, etc. are all of type "torch.tensortype".
            # torch.fx's tracer fails on these types, because it doesn't support arguments of torch.tensortype type.
            # So, we pass it in as a string (which is also supported, see above implementation for .type() with 0 args)
            tensor_type = dtype.as_python_constant()
            tensor_type_const = ConstantVariable.create(fqn(tensor_type))

            from ..symbolic_convert import InstructionTranslator
            from .builder import wrap_fx_proxy

            tx = InstructionTranslator.current_tx()

            if non_blocking:
                kwargs = {"non_blocking": non_blocking, **kwargs}

            return wrap_fx_proxy(
                tx,
                tx.output.create_proxy(
                    "call_method",
                    "type",
                    *proxy_args_kwargs([self, tensor_type_const], kwargs),
                ),
            )

    def method_as_subclass(self, cls):
        if isinstance(cls, TensorSubclassVariable) and cls.source:
            from ..symbolic_convert import InstructionTranslator
            from .builder import VariableBuilder
            from .torch_function import TensorWithTFOverrideVariable

            tx = InstructionTranslator.current_tx()

            # [Note: __torch_function__] coerce this tensor variable into a TensorWithTFOverrideVariable
            # in eager, this is just a type change. This isn't sound if a __torch_function__ tensor subclass
            # defines a constructor, but if only a __torch_function__ impl is defined, this is okay to call.
            # It is up to the user whether this is correct behavior or not.
            py_cls = cls.as_python_constant()
            torch_fn = VariableBuilder(
                tx,
                AttrSource(AttrSource(cls.source, "__torch_function__"), "__func__"),
            )(py_cls.__torch_function__.__func__)

            return TensorWithTFOverrideVariable.from_tensor_var(
                tx, self, py_cls, torch_fn
            )

    def method_get_device(self):
        if isinstance(self.device, torch.device):
            index = self.device.index if self.device.type != "cpu" else -1
            return ConstantVariable.create(index)

    def method_element_size(self):
        return ConstantVariable.create(self.dtype.itemsize)

    def method_numpy(self, *, force=False):
        if not config.trace_numpy:
            unimplemented("Tensor.numpy(). config.trace_numpy is False")
        if not np:
            unimplemented("Tensor.numpy(). NumPy is not available")
        if self.layout != torch.strided:
            raise TypeError(
                f"can't convert {self.layout} layout tensor to numpy. Use Tensor.dense() first"
            )
        from ..symbolic_convert import InstructionTranslator

        tx = InstructionTranslator.current_tx()

        # We don't check that the tensor is on CPU when force is False, as this
        # allows us to execute NumPy code on CUDA. Same for requires_grad=True
        if force and force.as_python_constant():
            # If the user set force=True we try to preserve the semantics (no gradients, move to CPU...)
            t = self.call_method(tx, "detach", [], {})
            proxy = tx.output.create_proxy("call_method", "cpu", (t.as_proxy(),), {})
        else:
            # Hacky way to create a view of self that will be marked as NumpyNdarrayVariable
            proxy = tx.output.create_proxy(
                "call_method", "view_as", *proxy_args_kwargs([self, self], {})
            )
        return NumpyNdarrayVariable.create(tx, proxy)

    def method_tolist(self):
        from ..symbolic_convert import InstructionTranslator
        from .builder import SourcelessBuilder

        tx = InstructionTranslator.current_tx()

        def tolist(tensor, sub_proxy):
            def wrap(i, sub_proxy):
                return SymNodeVariable.create(
                    tx,
                    sub_proxy.item(),
                    sym_num=tx.output.shape_env.create_unbacked_symint(),
                )

            if tensor.dtype not in [
                torch.int8,
                torch.int16,
                torch.int32,
                torch.int64,
            ]:
                unimplemented("Input tensor for tolist must be an integer tensor")

            if tensor.dim() == 0:
                return wrap(tensor, sub_proxy)

            if tensor.dim() == 1:
                return [wrap(val, sub_proxy[i]) for i, val in enumerate(tensor)]

            return [
                tolist(sub_tensor, sub_proxy=sub_proxy[i])
                for i, sub_tensor in enumerate(tensor)
            ]

        tensor = self.as_proxy().node.meta["example_value"]
        out = tolist(tensor, self.as_proxy())
        return SourcelessBuilder()(tx, out)

    def method_backward(self, *args, **kwargs):
        unimplemented("Tensor.backward")

    def method_data_ptr(self, *args, **kwargs):
        unimplemented("Tensor.data_ptr")

    def method_item(self, *args, **kwargs):
        if not config.capture_scalar_outputs:
            unimplemented("Tensor.item")

    def method___len__(self):
        from ..symbolic_convert import InstructionTranslator

        tx = InstructionTranslator.current_tx()
        return self.call_method(tx, "size", [ConstantVariable.create(0)], {})

    def method___setitem__(self, key, value):
        def has_bool_key(v):
            if isinstance(v, TensorVariable):
                return v.dtype in (torch.bool, torch.int8)
            elif isinstance(v, variables.TupleVariable):
                return any(has_bool_key(item) for item in v.items)
            else:
                return False

        if (
            has_bool_key(key)
            and isinstance(value, TensorVariable)
            and value.requires_grad
            and torch.is_grad_enabled()
        ):
            unimplemented(
                "boolean masking setitem backwards, see https://github.com/pytorch/pytorch/issues/114123"
            )
        from ..symbolic_convert import InstructionTranslator

        tx = InstructionTranslator.current_tx()
        tx.output.create_proxy(
            "call_function",
            operator.setitem,
            *proxy_args_kwargs([self, key, value], {}),
        )
        return ConstantVariable.create(None)

    def method_resize_(self, *args, **kwargs):
        unimplemented("Tensor.resize_")

    def method_resize_as_(self, *args, **kwargs):
        unimplemented("Tensor.resize_as_")

    def method_set_(self, *args, **kwargs):
        if len(args) > 1:
            # torch.Tensor.set_() has several overloads.
            # aten::set_.source_Tensor(Tensor) gets special handling
            # in AOTAutograd and functionalization, because it is the most common
            # overload and is used by FSDP.
            # graph-breaking on aten::set_source_Tensor_storage_offset for now,
            # unless we find that we need to make it work.
            unimplemented("Tensor.set_.source_Tensor_storage_offset")

    def method_add_(self, other, *, alpha=None):
        if alpha is not None:
            from ..symbolic_convert import InstructionTranslator

            tx = InstructionTranslator.current_tx()
            result = variables.TorchInGraphFunctionVariable(torch.mul).call_function(
                tx, [other, alpha], {}
            )
            return self.call_method(tx, "add_", [result], {})

    def method_addcdiv_(self, tensor1, tensor2, *, value=None):
        from ..symbolic_convert import InstructionTranslator

        tx = InstructionTranslator.current_tx()
        if value is not None:
            result = variables.TorchInGraphFunctionVariable(torch.div).call_function(
                tx, [tensor1, tensor2], {}
            )
            result = variables.TorchInGraphFunctionVariable(torch.mul).call_function(
                tx, [result, value], {}
            )
            return self.call_method(tx, "add_", [result], {})

    def method___contains__(self, arg):
        from ..symbolic_convert import InstructionTranslator

        tx = InstructionTranslator.current_tx()

        # Rewrite __contains__ here so that downstream passes can trace through
        # without dealing with unbacked symbool. Roughly the code we translate is:
        # def __contains__(self, x):
        #     return (x == self).any().item()
        result = variables.TorchInGraphFunctionVariable(torch.eq).call_function(
            tx, [self, arg], {}
        )
        result = variables.TorchInGraphFunctionVariable(torch.any).call_function(
            tx, [result], {}
        )
        return result.call_method(tx, "item", [], {})

    def method_redistribute(self, *args, **kwargs):
        from ..symbolic_convert import InstructionTranslator

        tx = InstructionTranslator.current_tx()
        # rewrite non-primitive args/kwargs to be included in the on-the-fly prim function
        # and rewrite args to have only proxyable args, then insert call_function
        args_as_value = [x.as_python_constant() for x in args]
        kwargs_as_value = {k: v.as_python_constant() for k, v in kwargs.items()}

        def redistribute_fn_with_prim_types(x):
            return x.redistribute(*args_as_value, **kwargs_as_value)

        # attach the same function name for better debugging
        redistribute_fn_with_prim_types.__name__ = "prim_redistribute"

        from .builder import wrap_fx_proxy

        return wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                redistribute_fn_with_prim_types,
                *proxy_args_kwargs([self], {}),
            ),
        )

    def method_register_hook(self, *args, **kwargs):
        return self._method_register_hook("register_hook", *args, **kwargs)

    def method_register_post_accumulate_grad_hook(self, *args, **kwargs):
        return self._method_register_hook(
            "register_post_accumulate_grad_hook", *args, **kwargs
        )

    def _method_register_hook(self, name, hook):
        # Note - do not arbitrarily add hooks here - make sure they match the same contract
        # see [On tensor.register_hook]
        from ..symbolic_convert import InstructionTranslator

        tx = InstructionTranslator.current_tx()

        if not self.source:
            if not compiled_autograd.compiled_autograd_enabled:
                # TODO(voz):
                # We can relax this by speculating the callable and ensuring that it doesn't modify arbitrary
                # python state.
                # We *Must* be in compiled_autograd here because backward hooks can contain anything, and it is unsafe to run
                # them in a compiled bwd without re-entering dynamo as compiled_autograd does.
                #
                # Discussion point 1 - Should we bypass this if nopython/fullgraph = True?
                #   No. Because this was going to be a graph break anyway - this check does not
                # introduce new graph breaks where there were none.
                #
                # Discussion point 2 - Should we defer this check to backwards?
                #   No. Because compiled autograd is not yet ready for prime time. As such, if we defer, a user
                # would have no recourse - their forward traces just fine, but will fail at backwards unless
                # compiled_autograd is enabled. If compiled_autograd fails (there are a lot of failures today)
                # then they have nothing they can do except disable compile.
                unimplemented(
                    "Compilation of intermediate hooks requires compiled autograd"
                )

            # This wraps our user provided fn with a function that intercedes and
            # uses our `invoke` higher order op to record a hook invocation in bwd graph.
            fn = functools.partial(trace_wrapped, fn=hook.guard_as_python_constant())

            def _register_hook_trampoline(tensor):
                hook_callable = getattr(tensor, name)
                hook_callable(fn)
                return tensor

            from .builder import wrap_fx_proxy

            return wrap_fx_proxy(
                tx,
                tx.output.create_proxy(
                    "call_function",
                    _register_hook_trampoline,
                    (self.as_proxy(),),
                    {},
                ),
            )

        handle_variable = variables.RemovableHandleVariable(
            mutable_local=variables.base.MutableLocal(),
        )
        tx.output.side_effects.register_hook(self, hook, handle_variable, name)
        return handle_variable

    def method_requires_grad_(self, requires_grad=True):
        if requires_grad is not True:
            requires_grad = requires_grad.as_python_constant()

        if self.as_proxy().node.meta["example_value"].requires_grad != requires_grad:
            unimplemented("Tensor.requires_grad_")
        else:
            return self

    def method_new(self, *args, **kwargs):
        # Convert x.new(torch.Size) into x.new_empty(torch.Size),
        # as Tensor.new acts differently with a Size input versus a tuple input.
        if len(args) == 1 and isinstance(args[0], SizeVariable):
            from ..symbolic_convert import InstructionTranslator

            return self.call_method(
                InstructionTranslator.current_tx(), "new_empty", args, kwargs
            )

    def rename(self, tx, name):
        self.proxy.node._rename(name)
        return super().rename(tx, name)


class SymNodeVariable(VariableTracker):
    """
    Represents a symbolic size, e.g., as returned by tensor.size(0)
    """

    @classmethod
    def create(cls, tx, proxy, sym_num, **options):
        if "example_value" in proxy.node.meta:
            assert proxy.node.meta["example_value"] == sym_num
        if sym_num is None:
            sym_num = get_fake_value(proxy.node, tx)
        proxy.node.meta["example_value"] = sym_num

        if isinstance(sym_num, (sympy.Integer, int, bool)):
            if isinstance(sym_num, sympy.Integer):
                breakpoint()
            sym_num = int(sym_num) if isinstance(sym_num, sympy.Integer) else sym_num
            return ConstantVariable.create(sym_num)

        return SymNodeVariable(proxy, sym_num, **options)

    def __init__(self, proxy, sym_num, **kwargs):
        super().__init__(**kwargs)
        self.proxy = proxy
        # TODO: Should we allow non SymTypes here?  Today it is allowed
        self.sym_num = sym_num

    def python_type(self):
        if isinstance(self.sym_num, SymTypes):
            return self.sym_num.node.pytype
        else:
            return type(self.sym_num)

    def as_proxy(self):
        return self.proxy

    def evaluate_expr(self, output_graph=None):
        try:
            return guard_scalar(self.sym_num)
        except GuardOnDataDependentSymNode as e:
            raise UserError(  # noqa: TRY200
                UserErrorType.ANTI_PATTERN,
                f"Consider annotating your code using torch._constrain_as_*(). {str(e)}",
                case_name="constrain_as_size_example",
            )

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        from .builder import wrap_fx_proxy

        return wrap_fx_proxy(
            tx,
            tx.output.create_proxy(
                "call_method",
                name,
                *proxy_args_kwargs([self] + list(args), kwargs),
            ),
        )


class NumpyNdarrayVariable(TensorVariable):
    """
    Represents a np.ndarray, but backed by torch Tensor via torch._numpy.ndarray.
    Use this for Tensor.numpy() call.
    """

    @staticmethod
    def create(tx, proxy, **options):
        from .builder import wrap_fx_proxy_cls

        return wrap_fx_proxy_cls(
            target_cls=NumpyNdarrayVariable,
            tx=tx,
            proxy=proxy,
            **options,
        )

    def var_getattr(self, tx, name):
        # NB: This INTENTIONALLY does not call super(), because there is
        # no intrinsic reason ndarray properties are related to Tensor
        # properties.  The inheritance here is for implementation sharing.

        from ..utils import numpy_attr_wrapper
        from .builder import wrap_fx_proxy

        result = None

        example_value = self.as_proxy().node.meta["example_value"]
        example_ndarray = tnp.ndarray(example_value)

        def insert_into_graph():
            return wrap_fx_proxy(
                tx,
                tx.output.create_proxy(
                    "call_function", numpy_attr_wrapper, (self.as_proxy(), name), {}
                ),
            )

        if name in ["T", "real", "imag"]:
            proxy = tx.output.create_proxy(
                "call_function",
                numpy_attr_wrapper,
                (self.as_proxy(), name),
                {},
            )
            result = NumpyNdarrayVariable.create(tx, proxy)

        # These are awkward to implement.  The standard playbook for torch._numpy
        # interop is to trace a call into the torch._numpy wrapper which works for
        # Tensor operations.  However, we don't want to do this for calls
        # that don't return Tensors, because in those cases we may not want
        # to trace the attribute access into the graph at all (it is sort
        # of harmless to do so, because AOTAutograd will eliminate them,
        # but it's best not to trace them in to begin with.)  But in any
        # case, tracing these into the graph is like trying to fit a square
        # peg into a round hole; best not to do it.  So instead we
        # painstakingly implement these by hand
        #
        # NB: only ALWAYS specialized attributes can go here; notably,
        # size/shape not allowed!
        elif name in ("ndim", "itemsize"):
            return ConstantVariable.create(getattr(example_ndarray, name))
        elif name in ("shape", "stride"):
            if not has_free_symbols(r := getattr(example_ndarray, name)):
                return ConstantVariable.create(tuple(int(r) for r in r))
            return insert_into_graph()
        elif name == "size":
            if not has_free_symbols(r := example_ndarray.size):
                return ConstantVariable.create(int(r))
            return insert_into_graph()
        elif name in ["base", "flags", "dtype"]:
            unimplemented(f"TODO: add support for ndarray.{name}")
        elif name in ["__version__"]:
            unimplemented("delegate np.__version__ to NumPy")
        if result is None:
            raise NotImplementedError()
        return result

    @staticmethod
    def patch_args(name, args, kwargs):
        if name == "clip":
            kwargs_rename = {"a_min": "min", "a_max": "max"}
            kwargs = {kwargs_rename.get(k, k): v for k, v in kwargs.items()}
        return args, kwargs

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        from ..utils import numpy_method_wrapper

        args, kwargs = self.patch_args(name, args, kwargs)

        if name in ["__len__", "size", "tolist"]:
            # delegate back to TensorVariable
            return super().call_method(tx, name, args, kwargs)
        if name == "tobytes":
            unimplemented("tobytes is not modelled in torch._numpy")
        proxy = tx.output.create_proxy(
            "call_function",
            numpy_method_wrapper(name),
            *proxy_args_kwargs([self] + list(args), kwargs),
        )
        return NumpyNdarrayVariable.create(tx, proxy)

    def python_type(self):
        return np.ndarray


class UnspecializedPythonVariable(TensorVariable):
    """
    This is a 1-element tensor represents unspecialized python float/int.
    """

    def __init__(
        self, proxy: torch.fx.Proxy, *, raw_value=None, need_unwrap=True, **kwargs
    ):
        super().__init__(proxy, **kwargs)
        self.raw_value = raw_value
        self.need_unwrap = need_unwrap

    @classmethod
    def from_tensor_variable(cls, tensor_variable, raw_value, need_unwrap=True):
        # Convert a `TensorVariable` instance into an `UnspecializedPythonVariable` instance.
        return UnspecializedPythonVariable(
            **dict(tensor_variable.__dict__),
            raw_value=raw_value,
            need_unwrap=need_unwrap,
        )


class FakeItemVariable(TensorVariable):
    """An unspecialized python variable which prevents access to the underlying raw value.
    This is needed if item is called on a FakeTensor."""

    def __init__(self, proxy: torch.fx.Proxy, **kwargs):
        need_unwrap = kwargs.pop("need_unwrap", False)
        super().__init__(proxy, **kwargs)
        self.need_unwrap = need_unwrap

    @classmethod
    def from_tensor_variable(cls, tensor_variable):
        return FakeItemVariable(**dict(tensor_variable.__dict__))


class TensorSubclassVariable(VariableTracker):
    def __init__(self, value, *args, **kwargs):
        self.value = value
        super().__init__(*args, **kwargs)

    def call_function(
        self, tx, args: List[VariableTracker], kwargs: Dict[str, VariableTracker]
    ) -> VariableTracker:
        if len(args) == 1 and isinstance(args[0], TensorVariable):
            from .builder import VariableBuilder
            from .torch_function import TensorWithTFOverrideVariable

            torch_fn = VariableBuilder(
                tx, AttrSource(self.source, "__torch_function__")
            )(self.value.__torch_function__)

            return TensorWithTFOverrideVariable.from_tensor_var(
                tx, args[0], self.value, torch_fn
            )

        return super().call_function(tx, args, kwargs)

    def as_python_constant(self):
        return self.value

    def python_type(self):
        return type(self.value)
