import warnings

__version__ = "0.1.0"

# Passlib still imports the stdlib `crypt` module, which raises a noisy warning on
# Python 3.12+. Filter it until we upgrade to a version that removes the dependency.
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    module=r"passlib\.utils",
    message=r"'crypt' is deprecated",
)
