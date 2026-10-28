"""Send requests through the Message Batches API (half price) and wait for them. Used by the agent panel."""
import time

WAIT_HOURS = 5  # the weekly workflow's limit is 6; batches can take up to 24


def run(client, requests, poll=60, wait_hours=WAIT_HOURS):
    """{custom_id: result} where result.type is succeeded, errored, canceled or expired. Cancels and stops loudly
    if the batch hasn't ended within wait_hours."""
    if not requests:
        return {}
    b = client.messages.batches.create(requests=requests).id
    deadline = time.time() + wait_hours * 3600
    while client.messages.batches.retrieve(b).processing_status != "ended":
        if time.time() > deadline:
            client.messages.batches.cancel(b)
            raise SystemExit(f"batch {b} not finished after {wait_hours} hours; canceled")
        time.sleep(poll)
    return {r.custom_id: r.result for r in client.messages.batches.results(b)}
