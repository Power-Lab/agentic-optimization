"""Adapters package. Importing it registers every bundled adapter so the
framework's registry can resolve them. Each model is a case study, not the
framework — village is the first."""

from adapters import village  # noqa: F401  (import triggers self-registration)
