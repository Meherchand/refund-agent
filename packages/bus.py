"""RabbitMQ topology and helpers.

Declared in code, by whoever connects, at startup. Not in a definitions file:
importing definitions on a blank node suppresses default user creation and the
broker boots with zero accounts (PLANNING-LOCAL.md §11).

Queues are **quorum** so `x-delivery-limit` is honoured — classic queues do not
track redelivery counts server-side, so a nack-with-requeue loops forever.
"""

from __future__ import annotations

import json
import os
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection

EXCHANGE = "refund"
DLX = "refund.dlx"
DELIVERY_LIMIT = 3

# queue -> (bound routing key, dead-letter routing key)
QUEUES: dict[str, tuple[str, str]] = {
    "case-events": ("case.submitted", "case.dead"),
    "refund-execute": ("refund.approved", "refund.dead"),
}


async def connect() -> AbstractRobustConnection:
    return await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])


async def declare(channel: AbstractChannel) -> dict[str, AbstractQueue]:
    """Idempotent. Every service calls this; the first one wins, the rest no-op."""
    ex = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.TOPIC, durable=True)
    queues: dict[str, AbstractQueue] = {}
    for name, (rk, dead_rk) in QUEUES.items():
        dlq = await channel.declare_queue(f"{name}.dlq", durable=True)
        await dlq.bind(dlx, dead_rk)
        q = await channel.declare_queue(
            name,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": DLX,
                "x-dead-letter-routing-key": dead_rk,
                "x-delivery-limit": DELIVERY_LIMIT,
            },
        )
        await q.bind(ex, rk)
        queues[name] = q
    return queues


async def publish(channel: AbstractChannel, routing_key: str, payload: dict[str, Any]) -> None:
    ex = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    await ex.publish(
        aio_pika.Message(
            json.dumps(payload).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=routing_key,
    )
