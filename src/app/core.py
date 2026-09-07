"""Core application logic."""


def greet(name: str = "world") -> str:
    """Return a greeting for ``name``.

    Args:
        name: Who to greet. Leading and trailing whitespace is stripped.

    Returns:
        A greeting string.

    Raises:
        ValueError: If ``name`` is empty or only whitespace.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("name must not be empty")
    return f"Hello, {cleaned}!"
