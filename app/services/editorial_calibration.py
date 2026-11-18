"""Optional import hook for user-supplied calibration records."""

def import_calibration(workflow, records=None):
    """Import only records explicitly supplied by the current project owner.

    The template intentionally ships without brand datasets or preference memory.
    """
    if records is None:
        return []
    raise NotImplementedError("Implement import for this project's approved calibration schema.")
