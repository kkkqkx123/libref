"""libref package entry point: the `libref` script points to cli.main."""

from libref.cli import main


def __getattr__(
    name: str,
):  # lazy export so importing libref does not pull in heavy dependencies
    if name == "main":
        return main
    raise AttributeError(name)
