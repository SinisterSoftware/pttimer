from pttimer import logic


def edit(ctx):
    """Edit the focused pttimer reservation."""
    logic.pttimer_edit(ctx)


def epg_search(ctx):
    """Search the pttimer EPG."""
    logic.pttimer_epg_search(ctx)
