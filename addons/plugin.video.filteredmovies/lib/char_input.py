# -*- coding: utf-8 -*-
"""Append an onscreen search letter only to its originating active window."""
import sys
import xbmcgui

if __name__ == '__main__' and len(sys.argv) > 2:
    try:
        target_id = int(sys.argv[2])
    except (ValueError, TypeError):
        target_id = -1
    if target_id == xbmcgui.getCurrentWindowId():
        target = xbmcgui.Window(target_id)
        if target.getProperty('MFG.SearchMode') == 'true':
            char = sys.argv[1].upper()
            if len(char) == 1 and char.isascii() and char.isalnum():
                target.setProperty('MFG.T9Input',
                                   target.getProperty('MFG.T9Input') + char)
