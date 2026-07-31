# PotatoFlow v1.5.39

This maintenance release fixes a common Internal Server Error on the Bilibili archive center.

- Fixes Jinja treating `category.items` as the dictionary method instead of the `items` field while rendering Bilibili message previews.
- Safely handles categories that omit the optional `items` field.
- Makes the Windows portable release workflow derive its version, archive name, checksum name, and health-check expectation from the project's canonical version file.

After upgrading, the Bilibili archive center should render normally instead of returning HTTP 500.
