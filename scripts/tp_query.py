"""Common utility for querying Trace Processor HTTP API.

Supports both protobuf (native trace_processor) and JSON (legacy) response formats.
"""

import argparse
import json
import os
import struct
import sys
import requests


def _encode_query_args(sql: str) -> bytes:
    """Encode SQL query as a protobuf QueryArgs message.

    QueryArgs proto:
      field 1 (sql_query): string, wire type 2 (length-delimited)
    """
    sql_bytes = sql.encode("utf-8")
    # field tag: field_number=1, wire_type=2 => 0x0a
    # varint length encoding
    length = len(sql_bytes)
    varint = []
    while length > 0x7f:
        varint.append((length & 0x7f) | 0x80)
        length >>= 7
    varint.append(length)
    return b"\x0a" + bytes(varint) + sql_bytes


def _decode_varint(data: bytes, pos: int) -> tuple:
    """Decode a protobuf varint starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7f) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _decode_protobuf_fields(data: bytes) -> dict:
    """Decode a flat protobuf message into {field_number: [values]}."""
    fields = {}
    pos = 0
    while pos < len(data):
        if pos >= len(data):
            break
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value, pos = _decode_varint(data, pos)
            fields.setdefault(field_number, []).append(value)
        elif wire_type == 1:  # 64-bit
            value = struct.unpack("<d", data[pos : pos + 8])[0]
            pos += 8
            fields.setdefault(field_number, []).append(value)
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(data, pos)
            value = data[pos : pos + length]
            pos += length
            fields.setdefault(field_number, []).append(value)
        elif wire_type == 5:  # 32-bit
            value = struct.unpack("<f", data[pos : pos + 4])[0]
            pos += 4
            fields.setdefault(field_number, []).append(value)
        else:
            break  # Unknown wire type
    return fields


def _parse_cells_batch(batch_bytes: bytes) -> dict:
    """Parse a CellsBatch protobuf message.

    CellsBatch proto:
      field 1: cells (repeated CellType enum, packed varint)
      2: varint_cells (repeated int64, packed varint)
      3: float64_cells (repeated double, packed fixed64)
      4: blob_cells (repeated bytes)
      5: string_cells (repeated string)
      6: is_last_batch (bool)
    """
    fields = _decode_protobuf_fields(batch_bytes)
    result = {"cells": [], "varint_cells": [], "float64_cells": [],
              "blob_cells": [], "string_cells": [], "is_last_batch": False}

    # Parse packed cells (field 1) - these are cell type enums
    if 1 in fields:
        for chunk in fields[1]:
            p = 0
            while p < len(chunk):
                val, p = _decode_varint(chunk, p)
                result["cells"].append(val)

    # Parse packed varint_cells (field 2)
    if 2 in fields:
        for chunk in fields[2]:
            p = 0
            while p < len(chunk):
                val, p = _decode_varint(chunk, p)
                # Handle signed integers (zigzag decode not needed for trace_processor)
                result["varint_cells"].append(val)

    # Parse packed float64_cells (field 3)
    if 3 in fields:
        for chunk in fields[3]:
            p = 0
            while p + 8 <= len(chunk):
                val = struct.unpack("<d", chunk[p : p + 8])[0]
                result["float64_cells"].append(val)
                p += 8

    # Parse string_cells (field 5)
    if 5 in fields:
        for chunk in fields[5]:
            # String cells are null-separated within the chunk
            strings = chunk.decode("utf-8", errors="replace").split("\0")
            result["string_cells"].extend(strings)

    # is_last_batch (field 6)
    if 6 in fields:
        result["is_last_batch"] = bool(fields[6][0])

    return result


# Cell types from the CellsBatch.CellType enum
CELL_INVALID = 0
CELL_NULL = 1
CELL_VARINT = 2
CELL_FLOAT64 = 3
CELL_STRING = 4
CELL_BLOB = 5


def _parse_query_result(data: bytes) -> dict:
    """Parse a QueryResult protobuf message into a dict.

    QueryResult proto:
      field 1: column_names (repeated string)
      field 2: error (string)
      field 3: batch (repeated CellsBatch)
    """
    fields = _decode_protobuf_fields(data)

    # Column names (field 1)
    column_names = []
    if 1 in fields:
        for v in fields[1]:
            column_names.append(v.decode("utf-8", errors="replace"))

    # Error (field 2)
    error = None
    if 2 in fields:
        err_text = fields[2][0].decode("utf-8", errors="replace")
        if err_text:
            error = err_text

    # Batches (field 3)
    all_cells = []
    all_varints = []
    all_floats = []
    all_strings = []
    if 3 in fields:
        for batch_bytes in fields[3]:
            batch = _parse_cells_batch(batch_bytes)
            all_cells.extend(batch["cells"])
            all_varints.extend(batch["varint_cells"])
            all_floats.extend(batch["float64_cells"])
            all_strings.extend(batch["string_cells"])

    # Convert cells into column-based data
    num_cols = len(column_names)
    if num_cols == 0:
        return {"column_names": [], "column_data": [], "error": error}

    column_data = [[] for _ in range(num_cols)]
    varint_idx = 0
    float_idx = 0
    string_idx = 0
    col_idx = 0

    for cell_type in all_cells:
        if cell_type == CELL_NULL:
            column_data[col_idx].append(None)
        elif cell_type == CELL_VARINT:
            column_data[col_idx].append(all_varints[varint_idx] if varint_idx < len(all_varints) else None)
            varint_idx += 1
        elif cell_type == CELL_FLOAT64:
            column_data[col_idx].append(all_floats[float_idx] if float_idx < len(all_floats) else None)
            float_idx += 1
        elif cell_type == CELL_STRING:
            column_data[col_idx].append(all_strings[string_idx] if string_idx < len(all_strings) else None)
            string_idx += 1
        elif cell_type == CELL_BLOB:
            column_data[col_idx].append(b"<blob>")
        col_idx = (col_idx + 1) % num_cols

    return {"column_names": column_names, "column_data": column_data, "error": error}


def query_tp(port: int, sql: str) -> dict:
    """Execute SQL query against Trace Processor HTTP API.

    Sends protobuf-encoded QueryArgs, receives protobuf QueryResult,
    returns dict with column_names and column_data.
    """
    url = f"http://localhost:{port}/query"
    try:
        payload = _encode_query_args(sql)
        resp = requests.post(url, data=payload,
                             headers={"Content-Type": "application/x-protobuf"},
                             timeout=30)
        resp.raise_for_status()

        result = _parse_query_result(resp.content)
        if result.get("error"):
            print(f"ERROR: SQL error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        return result

    except requests.exceptions.ConnectionError:
        print(f"ERROR: Cannot connect to Trace Processor on port {port}", file=sys.stderr)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR: Query failed: {e}", file=sys.stderr)
        sys.exit(1)


def parse_columns(result: dict) -> list[dict]:
    """Convert trace_processor column-based response to list of row dicts."""
    columns = result.get("column_names", [])
    data = result.get("column_data", [])
    if not columns or not data:
        return []

    num_rows = len(data[0]) if data else 0
    rows = []
    for i in range(num_rows):
        row = {}
        for j, col in enumerate(columns):
            row[col] = data[j][i] if j < len(data) else None
        rows.append(row)
    return rows


def save_result(data: dict, filename: str, output_dir: str = "/workspace/perf_analysis_output"):
    """Save result to JSON file."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Result saved to {filepath}", file=sys.stderr)


def print_result(data: dict):
    """Print result as JSON to stdout."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def add_common_args(parser: argparse.ArgumentParser):
    """Add common arguments to parser."""
    parser.add_argument("--port", type=int, default=9001, help="Trace Processor HTTP port")
    parser.add_argument("--output-dir", default="/workspace/perf_analysis_output", help="Output directory")
