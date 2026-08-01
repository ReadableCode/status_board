# %%
# Imports #

import os

# %%
# Variables #

file_dir = os.path.dirname(os.path.realpath(__file__))
parent_dir = os.path.dirname(file_dir)  # this repo's root
# The directory this repo is cloned into. Sibling ``*_credentials`` repos
# under it are where panel configs and host inventories are discovered.
grandparent_dir = os.path.dirname(parent_dir)

# %%

if __name__ == "__main__":
    print(f"file_dir: {file_dir}")
    print(f"parent_dir: {parent_dir}")
    print(f"grandparent_dir: {grandparent_dir}")

# %%
