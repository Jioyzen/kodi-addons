# coding: utf-8

import os
import struct
import xbmc
import xbmcvfs

class IMDBMapper:
    def __init__(self):
        # Path to the binary mapping file
        # Assuming it's placed in resources/data/ inside the addon
        addon_dir = xbmcvfs.translatePath('special://home/addons/metadata.tmdb.cn.optimization')
        self.bin_path = os.path.join(addon_dir, 'resources', 'data', 'tmdb_imdb_mapping.bin')
        self.rev_bin_path = os.path.join(addon_dir, 'resources', 'data', 'imdb_tmdb_mapping.bin')
        self._data = None
        self._rev_data = None
        self._loaded = False
        
    def _ensure_loaded(self, reverse=False):
        if reverse:
             if self._rev_data is not None: return
        else:
             if self._data is not None: return

        if not self._loaded:
            # Mark as loaded generally, but we check specific data below
            self._loaded = True

        try:
            if not reverse and self._data is None:
                if os.path.exists(self.bin_path):
                    with open(self.bin_path, 'rb') as f:
                        self._data = f.read()
                    xbmc.log(f'[TMDB Scraper] Loaded mapping. Size: {len(self._data)} bytes', xbmc.LOGINFO)
                else:
                    xbmc.log(f'[TMDB Scraper] Mapping file not found: {self.bin_path}', xbmc.LOGDEBUG)
                    self._data = False # Mark as attempted but failed

            if reverse and self._rev_data is None:
                if os.path.exists(self.rev_bin_path):
                    with open(self.rev_bin_path, 'rb') as f:
                        self._rev_data = f.read()
                    xbmc.log(f'[TMDB Scraper] Loaded reverse mapping. Size: {len(self._rev_data)} bytes', xbmc.LOGINFO)
                else:
                    xbmc.log(f'[TMDB Scraper] Reverse mapping file not found: {self.rev_bin_path}', xbmc.LOGDEBUG)
                    self._rev_data = False # Mark as attempted but failed
                    
        except Exception as e:
            xbmc.log(f'[TMDB Scraper] Failed to load mapping: {e}', xbmc.LOGERROR)

    def get_imdb_id(self, tmdb_id):
        """
        Get IMDB ID for a given TMDB ID.
        :param tmdb_id: int or str ID
        :return: 'ttxxxxxxx' string, or None
        """
        self._ensure_loaded(reverse=False)
        
        if not self._data:
            return None
            
        try:
            tid = int(tmdb_id)
        except (ValueError, TypeError):
            return None
            
        # Calculate offset: index * 4 bytes
        offset = tid * 4
        
        # Check boundary
        if offset < 0 or offset + 4 > len(self._data):
            return None
            
        # Read 4 bytes as unsigned int (Little Endian)
        # We use struct.unpack_from which avoids slicing bytes (saves memory/copy)
        val = struct.unpack_from('<I', self._data, offset)[0]
        
        if val == 0:
            return None
            
        # Format back to tt + 7 digits (or more if value is large)
        return 'tt{:07d}'.format(val)

    def get_tmdb_id(self, imdb_id):
        """
        Get TMDB ID for a given IMDB ID using Binary Search.
        :param imdb_id: string 'ttxxxxxxx'
        :return: str TMDB ID, or None
        """
        self._ensure_loaded(reverse=True)
        
        if not self._rev_data:
            return None
            
        if not imdb_id or not imdb_id.startswith('tt'):
            return None
            
        try:
            target_imdb = int(imdb_id[2:])
        except ValueError:
            return None
            
        # Binary Search on _rev_data (Elements are 8 bytes: [IMDB(4)][TMDB(4)])
        record_size = 8
        total_records = len(self._rev_data) // record_size
        
        low = 0
        high = total_records - 1
        
        while low <= high:
            mid = (low + high) // 2
            offset = mid * record_size
            
            # Read IMDB ID at mid
            mid_val = struct.unpack_from('<I', self._rev_data, offset)[0]
            
            if mid_val < target_imdb:
                low = mid + 1
            elif mid_val > target_imdb:
                high = mid - 1
            else:
                # Discovered match, read TMDB ID (next 4 bytes)
                tmdb_val = struct.unpack_from('<I', self._rev_data, offset + 4)[0]
                return str(tmdb_val)
                
        return None

# Singleton instance
_mapper = None

def get_imdb_id(tmdb_id):
    global _mapper
    if _mapper is None:
        _mapper = IMDBMapper()
    return _mapper.get_imdb_id(tmdb_id)

def get_tmdb_id(imdb_id):
    global _mapper
    if _mapper is None:
        _mapper = IMDBMapper()
    return _mapper.get_tmdb_id(imdb_id)

