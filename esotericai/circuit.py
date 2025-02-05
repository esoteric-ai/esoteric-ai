import asyncio
import json
import uuid
from typing import Any, Callable, Dict, List

import httpx
import websockets

class _Circuit:
    """
    Maintains the websocket connection and handles sending tasks and receiving
    completed tasks.
    """
    def __init__(self, ws_url: str, job_name: str):
        self.ws_url = ws_url
        self.job_name = job_name
        self.websocket = None
        self.pending_tasks: Dict[str, (asyncio.Future, dict)] = {}
        self.pending_acks: Dict[str, asyncio.Future] = {}  # NEW: To track start_job acks.
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self._stop = False
        self.connected_event = asyncio.Event()

    async def run_forever(self):
        # (Reconnection and sender/receiver logic remains unchanged.)
        attempt = 0
        schedule = [5] * 10 + [60] * 10 + [3600] * 24
        while not self._stop:
            try:
                print(f"Attempting connection to {self.ws_url} ...")
                async with websockets.connect(self.ws_url, max_size=None, ping_timeout=300) as websocket:
                    self.websocket = websocket
                    self.connected_event.set()
                    print("WebSocket connection established.")

                    await self._resend_pending_tasks()
                    receiver_task = asyncio.create_task(self.background_receiver())
                    sender_task = asyncio.create_task(self.batch_sender())
                    await websocket.wait_closed()
                    receiver_task.cancel()
                    sender_task.cancel()
                    await asyncio.gather(receiver_task, sender_task, return_exceptions=True)
            except Exception as e:
                print("WebSocket connection error:", e)
            finally:
                self.websocket = None
                self.connected_event.clear()
            if self._stop:
                break
            delay = schedule[attempt] if attempt < len(schedule) else schedule[-1]
            attempt += 1
            print(f"Reconnecting in {delay} seconds...")
            await asyncio.sleep(delay)
        print("Circuit run_forever stopped.")

    async def start_job(self):
        """
        Send a start_job message to the server to force a fresh start and wait for ack.
        """
        await self.connected_event.wait()
        # Create a request_id and a future to wait for an ack.
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        ack_future = loop.create_future()
        self.pending_acks[request_id] = ack_future

        # Send the start_job message including the request_id.
        await self.websocket.send(json.dumps({
            "action": "start_job",
            "request_id": request_id
        }))
        print("[Circuit] Sent start_job message to server, waiting for ack...")

        try:
            ack = await asyncio.wait_for(ack_future, timeout=30)
            print("[Circuit] Received start_job ack:", ack)
            return ack
        except asyncio.TimeoutError:
            print("[Circuit] Timeout waiting for start_job ack.")
            raise

    async def _resend_pending_tasks(self):
        """Re‑queue tasks that are still waiting for acknowledgment."""
        for task_id, (future, task_payload) in list(self.pending_tasks.items()):
            if not future.done():
                await self.task_queue.put(task_payload)

    async def background_receiver(self):
        """
        Continuously receive messages. When a 'return_tasks' or 'ack_start_job' message is received,
        resolve the corresponding pending future.
        """
        try:
            while not self._stop:
                message = await self.websocket.recv()
                data = json.loads(message)
                if not data:
                    continue
                action = data.get("action")
                if action == "return_tasks":
                    tasks = data.get("tasks", [])
                    for task in tasks:
                        task_id = task.get("id")
                        if task_id in self.pending_tasks:
                            future, _ = self.pending_tasks.pop(task_id)
                            if not future.done():
                                future.set_result(task)
                    # Send the ack back to the server.
                    request_id = data.get("request_id")
                    if request_id:
                        print("sending ack")
                        ack_payload = {
                            "action": "ack_returned",
                            "request_id": request_id,
                            "ack": True
                        }
                        await self.websocket.send(json.dumps(ack_payload))
                # NEW: Handle ack for start_job.
                elif action == "ack_start_job":
                    request_id = data.get("request_id")
                    if request_id and request_id in self.pending_acks:
                        future = self.pending_acks.pop(request_id)
                        if not future.done():
                            future.set_result(data.get("ack", False))
                else:
                    print("Received unhandled message:", data)
        except Exception as exc:
            print("background_receiver exception:", exc)

    async def batch_sender(self):
        """
        Periodically gather tasks from the task_queue and send them as one batch.
        If sending fails, re‑queue the tasks.
        """
        try:
            while not self._stop:
                await asyncio.sleep(0.1)
                batch = []
                while not self.task_queue.empty():
                    task_payload = self.task_queue.get_nowait()
                    batch.append(task_payload)
                if batch:
                    if not self.websocket:
                        # Re‑queue if not connected.
                        for task_payload in batch:
                            await self.task_queue.put(task_payload)
                        continue
                    try:
                        await self.websocket.send(json.dumps({
                            "action": "submit_tasks",
                            "tasks": batch
                        }))
                    except Exception as exc:
                        print("Error sending batch:", exc)
                        for task_payload in batch:
                            await self.task_queue.put(task_payload)
        except Exception as exc:
            print("batch_sender exception:", exc)

    async def send_chat_task(self, models: List[str], conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
        task_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        task_payload = {
            "id": task_id,
            "models": models,
            "conversation": conversation,
            "job_name": self.job_name,
        }
        self.pending_tasks[task_id] = (future, task_payload)
        await self.task_queue.put(task_payload)
        completed_task = await future
        return completed_task

    async def shutdown(self):
        self._stop = True
        if self.websocket:
            await self.websocket.close()


class TaskCircuit:
    """
    A per‑task handle for user code.
    """
    def __init__(self, circuit: _Circuit, index: int):
        self._circuit = circuit
        self.index = index

    async def chat(self, models: List[str], conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._circuit.send_chat_task(models, conversation)

class CircuitForBatchProcessing:
    """
    Public interface for launching multiple tasks.
    """
    @classmethod
    def dispatch(
        cls,
        job_name: str,
        task_func: Callable[[TaskCircuit, int], "asyncio.Future"],
        num_tasks: int,
        api_url: str  # e.g., "127.0.0.1:8000"
    ) -> None:
        asyncio.run(cls._dispatch(job_name, task_func, num_tasks, api_url))

    @classmethod
    async def _dispatch(
        cls,
        job_name: str,
        task_func: Callable[[TaskCircuit, int], "asyncio.Future"],
        num_tasks: int,
        api_url: str
    ):
        # Bind the job.
        bind_url = f"http://{api_url}/client/bind"
        ws_url_template = f"ws://{api_url}/client/ws/{{client_uid}}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(bind_url, json={"job_name": job_name})
            resp.raise_for_status()
            data = resp.json()
            client_uid = data["client_uid"]
            print(f"Bound to job '{job_name}' as client {client_uid}")

        ws_url = ws_url_template.format(client_uid=client_uid)
        circuit = _Circuit(ws_url, job_name)
        circuit_task = asyncio.create_task(circuit.run_forever())

        # Send the start_job message to cancel any prior execution.
        await circuit.start_job()

        user_tasks = []
        for i in range(num_tasks):
            task_circuit = TaskCircuit(circuit, i)
            user_tasks.append(asyncio.create_task(task_func(task_circuit, i)))
        try:
            await asyncio.gather(*user_tasks)
        except Exception as e:
            print("Exception in user tasks:", e)
        finally:
            await circuit.shutdown()
            await circuit_task

        print("All tasks completed. CircuitForBatchProcessing shutdown.")
