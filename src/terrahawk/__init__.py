"""
Terrahawk — Bird's eye view of your infra

A comprehensive Terragrunt infrastructure scanning tool that runs
terragrunt plan across all units in parallel and generates an
interactive HTML report with drift detection, architecture diagrams,
module introspection, resource tagging, and state age tracking.

Made with <3 by WeCloud.
"""

__version__ = "1.7.0"

from .cli import main

__all__ = ["main", "__version__"]
