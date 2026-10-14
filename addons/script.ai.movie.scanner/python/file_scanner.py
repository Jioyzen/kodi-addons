# -*- coding: UTF-8 -*-
"""File scanner - recursively find video files in a directory."""

import os
from path_display import redact_text
import xbmc
import xbmcvfs

ADDONID = 'script.ai.movie.scanner'

VIDEO_EXTENSIONS = {
    '.m4v', '.3g2', '.3gp', '.nsv', '.tp', '.ts', '.ty', '.strm',
    '.pls', '.rm', '.rmvb', '.mpd', '.ifo', '.mov',
    '.qt', '.divx', '.xvid', '.bivx', '.vob', '.iso',
    '.udf', '.pva', '.wmv', '.asf', '.asx', '.ogm', '.m2v', '.avi',
    '.dat', '.mp4', '.mkv', '.mk3d', '.avc',
    '.vp3', '.svq3', '.nuv', '.viv', '.dv', '.fli', '.flv',
    '.wpl', '.xspf', '.vdr', '.dvr-ms', '.xsp', '.mts', '.m2t',
    '.m2ts', '.evo', '.ogv', '.sdp', '.avs', '.rec', '.pxml',
    '.vc1', '.h264', '.rcv', '.rss', '.mpls', '.mpl', '.webm',
    '.wtv', '.trp', '.f4v'
}


def _log(level, msg):
    xbmc.log(msg='%s: %s' % (ADDONID, redact_text(msg)), level=level)


class ScanIncomplete(RuntimeError):
    pass


def find_video_files(root_path,cancelled=lambda:False):
    """Recursively find all video files under root_path using xbmcvfs.
    Detects BDMV structures and returns parent folder path (like Kodi).
    Returns a list of full paths.
    """
    video_files = []

    def _is_bdmv(path):
        """Check if path contains a valid BDMV structure with INDEX.BDMV."""
        try:
            bdmv_path = path.rstrip('/') + '/BDMV/'
            files = xbmcvfs.listdir(bdmv_path)[1]
            if any(f.lower() == 'index.bdmv' for f in files):
                # Also verify it's not BACKUP/INDEX.BDMV
                return True
        except Exception:
            pass
        return False

    def _scan(path,depth=0):
        if cancelled():
            raise ScanIncomplete('Directory traversal cancelled')
        if depth>128:
            raise ScanIncomplete('Directory depth exceeded')
        try:
            dirs, files = xbmcvfs.listdir(path)
        except Exception as e:
            _log(xbmc.LOGERROR, 'Error listing %s: %s' % (path, e))
            raise ScanIncomplete('Directory listing failed') from e
        if not dirs and not files and not xbmcvfs.exists(path):
            raise ScanIncomplete('Directory unavailable')

        # Check for BDMV structure in this directory
        if any(d.lower() == 'bdmv' for d in dirs) and _is_bdmv(path):
            # Found Blu-ray structure → use parent folder as video entry (no trailing /)
            bdmv_entry = path.rstrip('/')
            video_files.append(bdmv_entry)
            _log(xbmc.LOGINFO, 'BDMV detected: %s' % bdmv_entry)
            return  # skip deeper scanning

        # Process files
        for f in files:
            _, ext = os.path.splitext(f)
            if ext.lower() in VIDEO_EXTENSIONS and not f.startswith('._'):
                # xbmcvfs.listdir returns just filenames, build full path
                full_path = path.rstrip('/') + '/' + f
                video_files.append(full_path)

        # Recurse into subdirectories
        for d in dirs:
            if d.startswith('.'):
                continue
            if d.lower() in ('bdmv', 'certificate', 'auxdata', 'backup'):
                continue
            sub_path = path.rstrip('/') + '/' + d + '/'
            _scan(sub_path,depth+1)

    # Ensure directory has trailing slash for xbmcvfs
    if not root_path.endswith('/'):
        root_path += '/'
    _scan(root_path)
    _log(xbmc.LOGINFO, 'Found %d video files in %s' % (len(video_files), root_path))
    return video_files
