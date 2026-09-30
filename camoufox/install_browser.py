"""Install the explicitly selected browser at image build time, then verify it."""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    from camoufox.__main__ import CamoufoxUpdate, load_repo_cache
    from camoufox.pkgman import AvailableVersion, RepoConfig, Version, installed_verstr

    expected = os.environ["EXPECTED_BROWSER_VERSION"]
    subprocess.run([sys.executable, "-m", "camoufox", "sync"], check=True)
    subprocess.run([sys.executable, "-m", "camoufox", "set", f"official/prerelease/{expected}"], check=True)
    repo = next(item for item in load_repo_cache()["repos"] if item["name"].lower() == "official")
    release = next(item for item in repo["versions"] if f"{item['version']}-{item['build']}" == expected)
    selected = AvailableVersion(
        version=Version(release["build"], release["version"]), url=release["url"],
        is_prerelease=release.get("is_prerelease", False), sha256=release.get("sha256"),
        asset_created_at=release.get("created_at"),
    )
    # The pinned Dockerfile version is deliberate; no interactive build prompt.
    CamoufoxUpdate(repo_config=RepoConfig.find_by_name("Official"), selected_version=selected).update(
        i_know_what_im_doing=True,
    )
    actual = installed_verstr()
    if actual != expected:
        raise RuntimeError(f"Browser pin mismatch: {actual} != {expected}")
    print(f"Verified browser pin: {actual}")


if __name__ == "__main__":
    main()
