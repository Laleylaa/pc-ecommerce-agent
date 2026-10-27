"""B21: one real chat on a new DB containing only a copy of the current catalog.

Read-only source snapshot; no source users, orders, sessions or credentials copied.
Writes a new isolated database and trace/evidence. Never retries the chat or drops DBs.
"""

import argparse
import asyncio
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from secrets import token_urlsafe

import httpx

from tests.e2e import check, exposure, run
from tests.e2e.isolated import LOGIN, isolated_url, launch, snapshot, wait_server
from tests.e2e.suite import ROOT, digest, load_cases, now, read, write


async def copy_catalog(source: str, database: str, password: str) -> dict:
    from app.models import (
        AppUser,
        AttributeKey,
        AttributeValue,
        Base,
        Brand,
        Category,
        GoodsAttributeRelation,
        Sku,
        Spu,
        UserAuthCredential,
    )
    from app.services.auth import PasswordHasher
    from sqlalchemy import insert, select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    target = isolated_url(source, database)  # Validate before any writes.
    models = [Category, Brand, AttributeKey, AttributeValue, Spu, Sku, GoodsAttributeRelation]
    origin = create_async_engine(source)
    catalog = {}
    try:
        async with origin.connect() as connection:
            await connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            await connection.execute(text('SET TRANSACTION READ ONLY'))
            for model in models:
                catalog[model.__tablename__] = [dict(row) for row in (
                    await connection.execute(select(model.__table__))
                ).mappings()]
            await connection.rollback()
    finally:
        await origin.dispose()
    admin = create_async_engine(source, isolation_level='AUTOCOMMIT')
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await admin.dispose()
    engine = create_async_engine(target)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for model in models:
                rows = catalog[model.__tablename__]
                # The live schema uses timezone-aware audit dates in some tables;
                # Base.metadata's test schema has naive UTC dates. Preserve the
                # instant when copying into those columns, without changing facts.
                for row in rows:
                    for column in model.__table__.columns:
                        value = row.get(column.name)
                        if (isinstance(value, datetime) and value.tzinfo is not None
                                and getattr(column.type, 'timezone', None) is False):
                            row[column.name] = value.astimezone(UTC).replace(tzinfo=None)
                for offset in range(0, len(rows), 300):
                    await connection.execute(insert(model.__table__), rows[offset:offset + 300])
            await connection.execute(insert(AppUser), {
                'id': 1, 'login_identifier': LOGIN, 'display_name': 'B21合成测试账号',
                'status': 'active',
            })
            await connection.execute(insert(UserAuthCredential), {
                'user_id': 1, 'login_identifier': LOGIN,
                'password_hash': PasswordHasher.hash_password(password),
            })
    finally:
        await engine.dispose()
    # Independent oracle from raw database facts, not production matching code or model output.
    categories = {row['id']: row['name'] for row in catalog['category']}
    spus = {row['id']: row for row in catalog['spu']}
    eligible = []
    for row in catalog['sku']:
        spu = spus[row['spu_id']]
        specs = row['specs_json'] or {}
        wireless = specs.get('wireless') in ('是', True) or specs.get('connection_type') in (
            '蓝牙', '2.4G', '三模', '双模', '无线',
        )
        mechanical = '机械键盘' in spu['title'] or '机械键盘' in row['title']
        if (categories[spu['category_id']] == '键盘' and spu['status'] == row['status'] == 1
                and Decimal(row['price']) <= 500 and wireless and mechanical):
            eligible.append({
                'sku_id': row['id'], 'spu_id': row['spu_id'], 'title': row['title'],
                'price': str(row['price']), 'specs': specs,
            })
    return {'copied_counts': {key: len(rows) for key, rows in catalog.items()},
            'eligible': eligible, 'source_read_only': True,
            'oracle_rule': 'active keyboard, title mechanical keyboard, wireless spec, price<=500'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--database', required=True)
    parser.add_argument('--port', type=int, default=8019)
    parser.add_argument('--confirm-live-writes', action='store_true', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.core.config import get_settings

    settings = get_settings()
    target = isolated_url(settings.database_url, args.database)
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    password = token_urlsafe(24)
    facts = asyncio.run(copy_catalog(settings.database_url, args.database, password))
    write(folder / 'catalog-oracle.json', facts)
    baseline = asyncio.run(snapshot(target))
    write(folder / 'inventory.json', baseline)
    if len({item['spu_id'] for item in facts['eligible']}) < 2:
        raise ValueError('precondition not met: independently inspect catalog-oracle before chat')
    assert baseline['memory_count'] == 0 and not baseline['orders']
    write(folder / 'policy.json', read(ROOT / 'backend/data/knowledge_documents.json'))
    write(folder / 'environment.json', {
        'database': args.database, 'model': settings.llm_model, 'runtime': 'strands',
        'loop_trace_enabled': True, 'repeat': 1, 'created_at': now(),
        'isolation': 'current catalog copied; new synthetic user; no source user/order data',
        'original_reference': {'conversation_id': 977, 'run_id': 1508},
    })
    cases = load_cases(selected=['B21'])
    oracle = run.template(cases, 'normal')
    base_url = f'http://127.0.0.1:{args.port}/api'
    oracle['target'].update(
        base_url=base_url, user_id=1, model=settings.llm_model,
        server_revision='local-source-snapshot; isolated_server child process',
        dataset_revision=digest(folder / 'inventory.json'),
        valid_until=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        synthetic_account_only=True, initial_preferences_verified=True,
    )
    oracle['cases']['B21'].update(
        ready=True, verified_at=now(), verified_by='independent raw SQL catalog snapshot',
        facts=facts, evidence_files=['catalog-oracle.json', 'inventory.json', 'environment.json'],
        bindings={'eligible_sku_ids': [item['sku_id'] for item in facts['eligible']]},
    )
    write(folder / 'oracle.json', oracle)
    batch = folder / 'batch'
    run.prepare(cases, folder / 'oracle.json', batch, 1, 300)
    # Verify Phoenix before spending any model call; no replay on subsequent failure.
    with httpx.Client(trust_env=False, timeout=10) as client:
        client.get('http://127.0.0.1:6006/v1/projects').raise_for_status()
    process = launch(folder, 'normal', args.database, args.port)
    try:
        wait_server(process, base_url)
        with httpx.Client(trust_env=False, timeout=15) as client:
            response = client.post(base_url + '/auth/login', json={
                'login_identifier': LOGIN, 'password': password,
            })
            response.raise_for_status()
            os.environ['E2E_TOKEN'] = response.json()['access_token']
        print('B21: sending exactly one chat request; Phoenix enabled', flush=True)
        run.live(batch)
    finally:
        os.environ.pop('E2E_TOKEN', None)
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    after = asyncio.run(snapshot(target))
    write(batch / 'evidence/postflight.json', after)
    assert baseline == after, 'catalog/order/preferences changed during chat'
    exposure.collect(folder, 'http://127.0.0.1:6006')
    check.render(batch)
    print(f'B21 complete: {batch}; inspect raw trace and semantic checks', flush=True)


if __name__ == '__main__':
    main()
