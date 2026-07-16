import inspect
from functools import wraps
from typing import get_type_hints, Any
from cello._cello import Response

try:
    from pydantic import BaseModel, ValidationError
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False

def _validate_pydantic_params(pydantic_params, request, kwargs):
    """Shared validation logic for sync and async handlers.

    Returns:
        (kwargs, errors) tuple. If errors is non-empty, return 422 response.
    """
    json_body = None
    errors = []
    for name, model in pydantic_params.items():
        if name in kwargs:
            continue

        # Parse JSON once
        if json_body is None:
            try:
                json_body = request.json()
            except (ValueError, TypeError, UnicodeDecodeError, RuntimeError):
                errors.append({"loc": ["body"], "msg": "Invalid JSON body", "type": "value_error.json"})
                break

        try:
            instance = model.model_validate(json_body)
            kwargs[name] = instance
        except ValidationError as e:
            for err in e.errors():
                errors.append(err)
        except Exception as e:
            errors.append({"loc": [name], "msg": str(e), "type": "unknown"})

    return kwargs, errors


def _coerce_body(model, data):
    """Validate ``data`` against ``model``. Returns (instance, errors)."""
    try:
        if HAS_PYDANTIC and isinstance(model, type) and issubclass(model, BaseModel):
            return model.model_validate(data), None
        # Plain class / dataclass / framework DTO: construct from the mapping.
        if isinstance(data, dict):
            return model(**data), None
        return model(data), None
    except Exception as e:  # noqa: BLE001 - surface any validation failure as 400
        if HAS_PYDANTIC and isinstance(e, ValidationError):
            return None, e.errors()
        return None, [{"loc": ["body"], "msg": str(e), "type": "value_error"}]


def wrap_handler_with_body(handler, model):
    """Wrap a handler so the JSON request body is parsed and validated against
    ``model`` before the handler runs.

    On success the validated instance is injected into the handler (into the
    parameter annotated with ``model`` if present, otherwise the first
    non-``request`` parameter). On failure a ``400`` response is returned with a
    ``{"detail": [...]}`` body. Works with Pydantic models, dataclasses, and
    plain classes constructible from the decoded JSON.
    """
    # Decide which parameter receives the validated body.
    target = None
    try:
        sig = inspect.signature(handler)
        hints = get_type_hints(handler)
        params = [n for n in sig.parameters if n != "request"]
        for name in params:
            if hints.get(name) is model:
                target = name
                break
        if target is None and params:
            target = params[0]
    except (TypeError, ValueError, NameError):
        pass

    def _validate(request):
        try:
            data = request.json()
        except (ValueError, TypeError, UnicodeDecodeError, RuntimeError):
            return None, Response.json(
                {"detail": [{"loc": ["body"], "msg": "Invalid JSON body", "type": "value_error.json"}]},
                status=400,
            )
        instance, errors = _coerce_body(model, data)
        if errors:
            return None, Response.json({"detail": errors}, status=400)
        return instance, None

    if inspect.iscoroutinefunction(handler):
        @wraps(handler)
        async def async_wrapper(request, *args, **kwargs):
            instance, err = _validate(request)
            if err is not None:
                return err
            if target is not None:
                kwargs.setdefault(target, instance)
            else:
                args = (instance, *args)
            return await handler(request, *args, **kwargs)
        return async_wrapper

    @wraps(handler)
    def wrapper(request, *args, **kwargs):
        instance, err = _validate(request)
        if err is not None:
            return err
        if target is not None:
            kwargs.setdefault(target, instance)
        else:
            args = (instance, *args)
        return handler(request, *args, **kwargs)
    return wrapper


def wrap_handler_with_validation(handler):
    """
    Wrap a handler with Pydantic validation if type hints are present.
    Supports both sync and async handlers.
    """
    if not HAS_PYDANTIC:
        return handler

    try:
        # get_type_hints is more reliable than signature.parameters for resolved types
        type_hints = get_type_hints(handler)
        sig = inspect.signature(handler)
    except (TypeError, ValueError, NameError):
        # If we can't inspect (e.g. built-in or unresolvable type hints), just return
        return handler

    # Identify Pydantic params
    pydantic_params = {}

    for name, param in sig.parameters.items():
        if name in type_hints:
            annotation = type_hints[name]
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                pydantic_params[name] = annotation

    if not pydantic_params:
        return handler

    # Create async wrapper for async handlers
    if inspect.iscoroutinefunction(handler):
        @wraps(handler)
        async def async_wrapper(request, *args, **kwargs):
            kwargs, errors = _validate_pydantic_params(pydantic_params, request, kwargs)
            if errors:
                return Response.json({"detail": errors}, status=422)
            return await handler(request, *args, **kwargs)
        return async_wrapper

    # Sync wrapper for sync handlers
    @wraps(handler)
    def wrapper(request, *args, **kwargs):
        kwargs, errors = _validate_pydantic_params(pydantic_params, request, kwargs)
        if errors:
            return Response.json({"detail": errors}, status=422)
        return handler(request, *args, **kwargs)

    return wrapper
