# example_usage.py

```python
import asyncio
import time
from esotericai.circuit import CircuitForBatchProcessing

async def task(circuit, id):
    conv = [
        {
            "role": "user",
            "content": f"Hello! Please use number {id} in your responce."  
        }
    ]
    result = await circuit.chat(["llama8b"], conv)
    print(f"Task {id}/1 done:" + str(result))

start_time = time.time()

CircuitForBatchProcessing.dispatch(
        job_name="my_chat_job",
        task_func=task,
        num_tasks=5,
        api_url="127.0.0.1:6325"  # Nexus IP adress
)

end_time = time.time()

print(f"Total time taken: {end_time - start_time:.2f} seconds")
```