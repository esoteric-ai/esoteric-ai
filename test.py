# test.py

import asyncio
import time
from esotericai.circuit import CircuitForBatchProcessing

async def task(circuit, id):
    # First char round:
    conv = [
        {
            "role": "user",
            "content": f"Hello! You are bot №{5000 + id}."
        }
    ]
    result = await circuit.chat(["Mistral Small 3 (24B)"], conv)

    if "error" in result:
        print(f"Task {id} Error:" + str(result['error']))
    else:
        print(f"Task {id}/1 done:" + str(result))

start_time = time.time()

CircuitForBatchProcessing.dispatch(
        job_name="my_chat_job",
        task_func=task,
        num_tasks=500,
        api_url="127.0.0.1:6325"
)

end_time = time.time()

print(f"Total time taken: {end_time - start_time:.2f} seconds")