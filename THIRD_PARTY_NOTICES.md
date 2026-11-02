# Third-party components

The original Python implementation in this repository was written independently.
The Zotero projects listed in the README and PLAN were studied for behavior and
architecture; their AGPL source is not bundled or translated into this code.

## Bundled browser graph library

- Cytoscape.js **v3.34.4**, MIT license.
- Source: <https://github.com/cytoscape/cytoscape.js/tree/v3.34.4>.
- Bundled file: `src/reference_topology/static/vendor/cytoscape.min.js`.
- SHA-256: `081591ab735b2b6a3b31a3c60f8f3a3e2fee8c92939e0c7820ad1208571b6cd8`.
- Copyright and license: `src/reference_topology/static/vendor/cytoscape-LICENSE.txt`.

Python dependencies are installed through `pyproject.toml` and retain their own
licenses. GROBID is an optional separately installed local service; it is not
distributed with this project. No third-party article PDFs are distributed.
