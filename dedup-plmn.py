#!/usr/bin/env python3

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from xml.parsers import expat

from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
    message_factory,
    text_format,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def plmn(attributes):
    mcc, mnc = attributes.get('mcc'), attributes.get('mnc')
    if not mcc or not mnc:
        return None
    if not re.fullmatch(r'[0-9]{3}', mcc) or not re.fullmatch(
        r'[0-9]{2,3}', mnc
    ):
        raise ValueError('Invalid MCC/MNC')
    return int(mcc), int(mnc)


def read_apns(content):
    parser = expat.ParserCreate()
    depth = 0
    rows = []
    start = None

    def start_element(name, attributes):
        nonlocal depth, start
        if depth == 0:
            if name != 'apns' or set(attributes) != {'version'}:
                raise ValueError('Expected an APN document with a version')
        elif depth == 1 and name == 'apn':
            start = parser.CurrentByteIndex
            rows.append((attributes, start, None))
        else:
            raise ValueError('Unsupported APN document structure')
        depth += 1

    def end_element(name):
        nonlocal depth
        depth -= 1
        if depth == 1:
            end = parser.CurrentByteIndex
            if content[end - 2 : end] != b'/>':
                end = content.index(b'>', end) + 1
            rows[-1] = (rows[-1][0], start, end)

    def text(value):
        if value.strip():
            raise ValueError('Unexpected text in APN document')

    def reject_doctype(*arguments):
        raise ValueError('DTDs and entities are not supported')

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.Parse(content, True)
    return rows


def carrier_networks(content):
    definition = descriptor_pb2.FileDescriptorProto(
        name='plmn.proto', syntax='proto2'
    )
    messages = {
        'CarrierAttribute': [('mccmnc_tuple', 1, 9, None, True)],
        'CarrierId': [
            ('canonical_id', 1, 5, None, False),
            ('carrier_attribute', 3, 11, '.CarrierAttribute', True),
        ],
        'CarrierList': [('carrier_id', 1, 11, '.CarrierId', True)],
    }
    for name, fields in messages.items():
        message = definition.message_type.add(name=name)
        for name, number, kind, type_name, repeated in fields:
            field = message.field.add(
                name=name,
                number=number,
                type=kind,
                label=3 if repeated else 1,
            )
            if type_name:
                field.type_name = type_name
    pool = descriptor_pool.DescriptorPool()
    pool.Add(definition)
    table = message_factory.GetMessageClass(
        pool.FindMessageTypeByName('CarrierList')
    )()
    try:
        text_format.Parse(
            content.decode('utf-8'), table, allow_unknown_field=True
        )
    except text_format.ParseError as error:
        raise ValueError('Invalid carrier ID table') from error
    networks = {}
    for carrier in table.carrier_id:
        identifier = carrier.canonical_id
        if identifier <= 0 or identifier in networks:
            raise ValueError('Invalid or duplicate carrier ID')
        networks[identifier] = set()
        for attributes in carrier.carrier_attribute:
            for numeric in attributes.mccmnc_tuple:
                if not re.fullmatch(r'[0-9]{3}(?:[0-9]{2,3})?', numeric):
                    raise ValueError('Invalid carrier MCC/MNC')
                if len(numeric) > 3:
                    networks[identifier].add(
                        (int(numeric[:3]), int(numeric[3:]))
                    )
    return networks


def remove_rows(content, rows, ordinals):
    for ordinal in sorted(ordinals, reverse=True):
        _, start, end = rows[ordinal - 1]
        line_start = content.rfind(b'\n', 0, start) + 1
        if not content[line_start:start].strip():
            start = line_start
        line_end = content.find(b'\n', end)
        if line_end != -1 and not content[end:line_end].strip():
            end = line_end + 1
        content = content[:start] + content[end:]
    expected = [
        attributes
        for number, (attributes, _, _) in enumerate(rows, 1)
        if number not in ordinals
    ]
    if [attributes for attributes, _, _ in read_apns(content)] != expected:
        raise ValueError('Row removal changed surviving APNs')
    return content if expected else None


def atomic_write(path, content, mode):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_changes(outputs, snapshots):
    for path, content in snapshots.items():
        if path.read_bytes() != content:
            raise ValueError(f'Input changed during review: {path}')
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in outputs}
    changed = []
    try:
        for path, content in outputs.items():
            if content is None:
                path.unlink()
            else:
                atomic_write(path, content, modes[path])
            changed.append(path)
    except OSError:
        for path in reversed(changed):
            atomic_write(path, snapshots[path], modes[path])
        raise


def run(args):
    paths = sorted(args.local_dir.glob('*.xml'))
    if not paths:
        raise ValueError('No local XML files found')
    inputs = [args.source, args.carrier_list, *paths]
    resolved = [path.resolve() for path in inputs]
    if len(set(resolved)) != len(resolved) or any(
        path.is_symlink() for path in inputs
    ):
        raise ValueError('Inputs must be distinct, non-symlink files')
    if args.report and (
        args.report.exists() or args.report.resolve() in resolved
    ):
        raise ValueError('Report must be a new file, not an input')
    snapshots = {path: path.read_bytes() for path in inputs}
    upstream = read_apns(snapshots[args.source])
    if not upstream:
        raise ValueError('Upstream APN file is empty')
    networks = carrier_networks(snapshots[args.carrier_list])
    explicit = set()
    carrier_coverage = {}
    unknown_ids = set()
    for attributes, _, _ in upstream:
        numeric = plmn(attributes)
        if numeric is not None:
            explicit.add(numeric)
        identifier = attributes.get('carrier_id')
        if identifier is None:
            continue
        identifier = int(identifier)
        if identifier <= 0:
            continue
        if identifier not in networks:
            unknown_ids.add(identifier)
        for numeric in networks.get(identifier, ()):
            carrier_coverage.setdefault(numeric, set()).add(identifier)
    removals = []
    outputs = {}
    local_count = 0
    for path in paths:
        rows = read_apns(snapshots[path])
        local_count += len(rows)
        selected = set()
        for ordinal, (attributes, _, _) in enumerate(rows, 1):
            numeric = plmn(attributes)
            if numeric is None or (
                numeric not in explicit and numeric not in carrier_coverage
            ):
                continue
            selected.add(ordinal)
            removals.append(
                {
                    'file': str(path),
                    'ordinal': ordinal,
                    'carrier': attributes.get('carrier'),
                    'mcc': attributes['mcc'],
                    'mnc': attributes['mnc'],
                    'reason': (
                        'same_plmn'
                        if numeric in explicit
                        else 'carrier_id_coverage'
                    ),
                    'upstream_carrier_ids': sorted(
                        carrier_coverage.get(numeric, ())
                    ),
                }
            )
        if selected:
            outputs[path] = remove_rows(snapshots[path], rows, selected)
    report = {
        'mode': 'applied' if args.apply else 'preview',
        'source': str(args.source),
        'carrier_list': str(args.carrier_list),
        'local_apns': local_count,
        'removed_apns': len(removals),
        'remaining_apns': local_count - len(removals),
        'changed_files': len(outputs),
        'empty_files': [
            str(path) for path, content in outputs.items() if content is None
        ],
        'unknown_upstream_carrier_ids': sorted(unknown_ids),
        'removals': removals,
    }
    if args.report:
        with args.report.open('x', encoding='utf-8') as output:
            json.dump(
                dict(report, mode='preview'),
                output,
                ensure_ascii=False,
                indent=2,
            )
            output.write('\n')
    if args.apply:
        apply_changes(outputs, snapshots)
        if args.report:
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
    if unknown_ids:
        print(
            f'Warning: {len(unknown_ids)} upstream carrier IDs are absent '
            'from the carrier table; only their explicit PLMNs are used',
            file=sys.stderr,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description=(
            'Remove local APNs on PLMNs covered by upstream, regardless '
            'of APN type or SIM conditions. Default: preview only.'
        )
    )
    parser.add_argument(
        '--source', type=Path, default=SCRIPT_DIR / 'upstream/apns-conf.xml'
    )
    parser.add_argument('--local-dir', type=Path, default=SCRIPT_DIR)
    parser.add_argument(
        '--carrier-list',
        type=Path,
        default=SCRIPT_DIR / '../../packages/providers/TelephonyProvider'
        '/assets/latest_carrier_id/carrier_list.textpb',
    )
    parser.add_argument(
        '--report',
        type=Path,
        help='Write a credential-free JSON report to a new file',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Delete matching APNs and files left empty',
    )
    args = parser.parse_args()
    try:
        run(args)
    except (OSError, ValueError, expat.ExpatError) as error:
        print(f'PLMN deduplication failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
