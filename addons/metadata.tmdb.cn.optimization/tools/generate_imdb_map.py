import pandas as pd
import struct
import sys
import os

def generate_map(csv_path, output_path):
    print(f"Reading {csv_path}...")
    # Read CSV
    # usecols to save memory
    df = pd.read_csv(csv_path, usecols=['id', 'imdb_id'], dtype={'id': 'Int64', 'imdb_id': str})
    
    # Drop NAs
    df = df.dropna()
    total_raw = len(df)
    
    # Filter 1: Must start with 'tt'
    df = df[df['imdb_id'].str.startswith('tt', na=False)]
    
    # Extract numeric part
    df['imdb_num_str'] = df['imdb_id'].str.slice(2)
    
    # Filter 2: Must be purely digits
    df = df[df['imdb_num_str'].str.isdigit()]
    
    # Filter 3: Length >= 7 (User confirmation: length 6 is invalid)
    df = df[df['imdb_num_str'].str.len() >= 7]
    
    # Convert to integers
    # uint32 is sufficient (max ~200 million or so, limit is 4 billion)
    df['imdb_int'] = df['imdb_num_str'].astype('uint32')
    df['tmdb_id'] = df['id'].astype('int32')
    
    # Determine array size
    # We must ensure we cover the max TMDB ID, even if intermediate IDs are missing.
    # The max_tmdb_id determines the array length.
    max_tmdb_id = df['tmdb_id'].max()
    print(f"Max TMDB ID: {max_tmdb_id}")
    print(f"Valid Mappings: {len(df)} (Original: {total_raw})")
    
    # Create bytearray (initialized to 0)
    # Since bytearray is initialized to \x00, any index not explicitly written to
    # will remain 0, representing "No IMDB ID".
    # Size = (max_id + 1) * 4 bytes
    array_size = (max_tmdb_id + 1) * 4
    print(f"Allocating {array_size / 1024 / 1024:.2f} MB...")
    
    buffer = bytearray(array_size)
    
    # Fill buffer
    # Ideally iterate fast. using zip is faster than iterrows
    print("filling buffer...")
    
    # We can use numpy for speed if available, but pure python struct is fine for offline tool
    count = 0
    for tmdb, imdb in zip(df['tmdb_id'], df['imdb_int']):
        # offset = tmdb * 4
        # write 4 bytes Little Endian (<I)
        struct.pack_into('<I', buffer, tmdb * 4, imdb)
        count += 1
        if count % 100000 == 0:
            print(f"Processed {count}...")
            
    print(f"Finished processing {count} records.")
    
    # Write to file
    with open(output_path, 'wb') as f:
        f.write(buffer)
        
    print(f"Successfully saved TMDB->IMDB map to {output_path}")

    # ---------------------------------------------------------
    # Generate Reverse Map: IMDB -> TMDB
    # Structure: [IMDB_ID(4 bytes)][TMDB_ID(4 bytes)] ... sorted by IMDB_ID
    # This allows binary search.
    # ---------------------------------------------------------
    reverse_output_path = output_path.replace('tmdb_imdb_mapping.bin', 'imdb_tmdb_mapping.bin')
    print(f"\nGeneratin Reverse Map (IMDB -> TMDB)...")
    
    # Sort by IMDB ID
    df_sorted = df.sort_values(by='imdb_int')
    
    print(f"Sorting {len(df_sorted)} records...")
    
    # Create buffer for reverse map: count * 8 bytes
    rev_buffer_size = len(df_sorted) * 8
    rev_buffer = bytearray(rev_buffer_size)
    
    print(f"Allocating {rev_buffer_size / 1024 / 1024:.2f} MB for reverse map...")
    
    offset = 0
    count = 0
    for tmdb, imdb in zip(df_sorted['tmdb_id'], df_sorted['imdb_int']):
        # Write IMDB ID then TMDB ID (8 bytes total)
        struct.pack_into('<II', rev_buffer, offset, imdb, tmdb)
        offset += 8
        count += 1
        
    with open(reverse_output_path, 'wb') as f:
        f.write(rev_buffer)
        
    print(f"Successfully saved IMDB->TMDB map to {reverse_output_path}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python generate_imdb_map.py <input.csv> <output.bin>")
    else:
        generate_map(sys.argv[1], sys.argv[2])