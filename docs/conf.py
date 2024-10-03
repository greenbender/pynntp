from sphinx_pyproject import SphinxConfig

project = SphinxConfig("../pyproject.toml", style="poetry", globalns=globals()).name
