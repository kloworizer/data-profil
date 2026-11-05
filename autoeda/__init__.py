"""AutoEDA: no-code automated exploratory data analysis."""
__version__ = "0.1.0"


def build_info() -> str:
    """Build stamp written into the image by the Dockerfile (BUILD arg) or "dev" outside an image.

    Air-gapped installations are updated by copying an app_image.tar.gz around; without a stamp there
    is no way to tell "the new bundle is installed" from "the old image was loaded again" - both answer
    /health with the same version number. The stamp is the build time, so a stale install is visible.
    """
    try:
        with open("/app/.build-info", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "dev"
