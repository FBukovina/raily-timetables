#!/usr/bin/env python3
"""Dependency-free GTFS Realtime decoder for TripUpdates.

Reads the protobuf wire format directly (field numbers from
gtfs-realtime.proto). Only trips the caller asks for have their stop time
updates decoded: Germany's feed carries 126 k trip updates, of which about
9 k are railway trips.
"""
from __future__ import annotations

SCHEDULED, SKIPPED, NO_DATA = 0, 1, 2
CANCELED = 3


def _varint(data, index):
    result = shift = 0
    while True:
        byte = data[index]; index += 1
        result |= (byte & 0x7F) << shift; shift += 7
        if byte < 0x80: return result, index


def _fields(data, start, end):
    index = start
    while index < end:
        key, index = _varint(data, index)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _varint(data, index); yield number, wire, value
        elif wire == 2:
            length, index = _varint(data, index); yield number, wire, (index, index + length); index += length
        elif wire == 1: yield number, wire, None; index += 8
        elif wire == 5: yield number, wire, None; index += 4
        else: raise ValueError(f'Unsupported protobuf wire type {wire}')


def _int64(value):
    return value - (1 << 64) if value >= 1 << 63 else value


def _text(data, bounds):
    return bytes(data[bounds[0]:bounds[1]]).decode('utf-8')


def _event(data, bounds):
    event = {}
    for number, wire, value in _fields(data, *bounds):
        if number == 1 and wire == 0: event['delay'] = _int64(value)
        elif number == 2 and wire == 0: event['time'] = _int64(value)
    return event


def decode(data, wanted=lambda trip_id: True):
    """Return (header timestamp, [trip update]) for trips `wanted` accepts.

    A trip update is {'trip_id', 'start_date', 'relationship', 'timestamp',
    'delay', 'stops': [{'sequence', 'stop_id', 'relationship', 'arrival',
    'departure'}]}, with absent fields left out.
    """
    data = memoryview(data)
    header_timestamp, updates = None, []
    for number, wire, value in _fields(data, 0, len(data)):
        if number == 1 and wire == 2:
            for field, kind, item in _fields(data, *value):
                if field == 3 and kind == 0: header_timestamp = item
        if number != 2 or wire != 2: continue
        for field, kind, entity in _fields(data, *value):
            if field != 3 or kind != 2: continue
            update, stops, accepted = {}, [], None
            for part, part_kind, item in _fields(data, *entity):
                if part == 1 and part_kind == 2:
                    for d_field, d_kind, d_value in _fields(data, *item):
                        if d_field == 1 and d_kind == 2: update['trip_id'] = _text(data, d_value)
                        elif d_field == 3 and d_kind == 2: update['start_date'] = _text(data, d_value)
                        elif d_field == 4 and d_kind == 0: update['relationship'] = d_value
                    accepted = wanted(update.get('trip_id'))
                    if not accepted: break
                elif part == 2 and part_kind == 2:
                    stop = {}
                    for s_field, s_kind, s_value in _fields(data, *item):
                        if s_field == 1 and s_kind == 0: stop['sequence'] = s_value
                        elif s_field == 4 and s_kind == 2: stop['stop_id'] = _text(data, s_value)
                        elif s_field == 5 and s_kind == 0: stop['relationship'] = s_value
                        elif s_field in (2, 3) and s_kind == 2:
                            stop['arrival' if s_field == 2 else 'departure'] = _event(data, s_value)
                    stops.append(stop)
                elif part == 4 and part_kind == 0: update['timestamp'] = item
                elif part == 5 and part_kind == 0: update['delay'] = _int64(item)
            # The trip descriptor normally comes first; check it again in case it didn't.
            if accepted is None: accepted = wanted(update.get('trip_id'))
            if accepted and update.get('trip_id'):
                update['stops'] = stops
                updates.append(update)
    return header_timestamp, updates
