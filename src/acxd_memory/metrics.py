# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CloudWatch metrics via the Embedded Metric Format (EMF).

Each call prints one EMF JSON line to stdout; Lambda ships it to the function's log group and
CloudWatch extracts the metric. No ``cloudwatch:PutMetricData`` permission is needed, and no
customer text is ever part of a metric (threat T5).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Mapping, TextIO

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "ACXDMemory")


def emit(
    name: str,
    value: float = 1,
    *,
    dimensions: Mapping[str, str] | None = None,
    unit: str = "Count",
    stream: TextIO | None = None,
) -> None:
    """Emit one metric. ``dimensions`` values must be low-cardinality (route, reason)."""
    dims = {"Function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local")}
    if dimensions:
        dims.update({k: str(v) for k, v in dimensions.items()})
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [sorted(dims)],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        **dims,
    }
    print(json.dumps(record, separators=(",", ":")), file=stream or sys.stdout, flush=True)
