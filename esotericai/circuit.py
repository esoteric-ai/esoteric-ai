# circuit.py
import asyncio
import json
import uuid
from typing import Any, Callable, Dict, List

import httpx
import websockets

class _Circuit:
    """
    Maintains the websocket connection and handles sending tasks (in batches)
    and receiving completed tasks from the server.
    """
    def __init__(self, websocket: websockets.WebSocketClientProtocol, job_name: str):
        self.websocket = websocket
        self.job_name = job_name

        # For matching task completions to local futures;
        # key: task_id (string), value: asyncio.Future
        self.pending_tasks: Dict[str, asyncio.Future] = {}

        # A queue for tasks to be sent to the server.
        self.task_queue: asyncio.Queue = asyncio.Queue()

        # To stop background tasks.
        self._stop = False

    async def background_receiver(self):
        """
        Reads messages from the websocket, e.g. "return_tasks" from the server.
        Each returned task has an 'id'; we match that to a local future.
        """
        try:
            while not self._stop:
                message = await self.websocket.recv()
                data = None
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    print("Received non-JSON message:", message)
                    continue

                if not data:
                    continue

                action = data.get("action")
                if action == "return_tasks":
                    # The server is returning a batch of completed tasks.
                    tasks = data.get("tasks", [])
                    for task in tasks:
                        task_id = task.get("id")
                        future = self.pending_tasks.pop(task_id, None)
                        if future and not future.done():
                            # Set the entire task object as the result.
                            future.set_result(task)
                    # Acknowledge the server.
                    request_id = data.get("request_id")
                    if request_id:
                        ack_payload = {
                            "action": "ack_returned",
                            "request_id": request_id,
                            "ack": True
                        }
                        await self.websocket.send(json.dumps(ack_payload))
                else:
                    print("Received unhandled message:", data)
        except Exception as exc:
            print("background_receiver exception:", exc)

    async def batch_sender(self):
        """
        Periodically gathers tasks from the task_queue and sends them in one "submit_tasks" message.
        """
        try:
            while not self._stop:
                await asyncio.sleep(0.1)

                batch = []
                while not self.task_queue.empty():
                    batch.append(self.task_queue.get_nowait())

                if batch:
                    payload = {"action": "submit_tasks", "tasks": batch}
                    try:
                        await self.websocket.send(json.dumps(payload))
                    except Exception as exc:
                        print("Error sending batch:", exc)
        except Exception as exc:
            print("batch_sender exception:", exc)

    async def send_chat_task(self, models: List[str], conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Creates a new task with a unique 'id'. Instead of a single model,
        the task has a list of models.
        Returns a future that completes when the task is processed.
        """
        task_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_tasks[task_id] = future

        task_payload = {
            "id": task_id,
            "models": models,           # <-- New list of models.
            "conversation": conversation,
        }

        # Enqueue the task for sending.
        await self.task_queue.put(task_payload)

        # Wait until the task is processed (via worker → server → client).
        completed_task = await future
        return completed_task

    async def shutdown(self):
        """Stop background tasks and close the websocket."""
        self._stop = True
        await self.websocket.close()


class TaskCircuit:
    """
    A per-task handle for your user code. It uses the parent _Circuit to submit chat tasks.
    """
    def __init__(self, circuit: _Circuit, index: int):
        self._circuit = circuit
        self.index = index

    async def chat(self, models: List[str], conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Send a single chat request as a new task. Note that
        `models` is now a list of strings.
        Returns the completed task (including its result).
        """
        task = await self._circuit.send_chat_task(models, conversation)
        return task


class CircuitForBatchProcessing:
    """
    Public interface for launching multiple tasks, each of which gets a TaskCircuit handle.
    """

    @classmethod
    def dispatch(
        cls,
        job_name: str,
        task_func: Callable[[TaskCircuit, int], "asyncio.Future"],
        num_tasks: int,
        api_url: str  # This is a required parameter.
    ) -> None:
        """
        Launches multiple tasks under the specified job name.
        
        Parameters:
            job_name (str): The name of the job.
            task_func (Callable): The async function to run for each task.
            num_tasks (int): The number of tasks to run.
            api_url (str): The base URL (host and port) for the API (e.g., "37.194.195.213:6325").
        """
        asyncio.run(cls._dispatch(job_name, task_func, num_tasks, api_url))

    @classmethod
    async def _dispatch(
        cls,
        job_name: str,
        task_func: Callable[[TaskCircuit, int], "asyncio.Future"],
        num_tasks: int,
        api_url: str
    ):
        # Construct the required endpoints using the provided api_url.
        # Here we assume that the API URL provided is in the form "host:port"
        bind_url = f"http://{api_url}/client/bind"
        ws_url_template = f"ws://{api_url}/client/ws/{{client_uid}}"

        # 1) Bind the job via HTTP.
        async with httpx.AsyncClient() as client:
            resp = await client.post(bind_url, json={"job_name": job_name})
            resp.raise_for_status()
            data = resp.json()
            client_uid = data["client_uid"]
            print(f"Bound to job '{job_name}' as client {client_uid}")

        # 2) Connect via WebSocket.
        ws_url = ws_url_template.format(client_uid=client_uid)
        async with websockets.connect(ws_url) as websocket:
            print("WebSocket connection established.")

            circuit = _Circuit(websocket, job_name)

            # Start background tasks.
            receiver_task = asyncio.create_task(circuit.background_receiver())
            sender_task = asyncio.create_task(circuit.batch_sender())

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
                receiver_task.cancel()
                sender_task.cancel()
                try:
                    await asyncio.gather(receiver_task, sender_task)
                except asyncio.CancelledError:
                    pass

            print("All tasks completed. CircuitForBatchProcessing shutdown.")
