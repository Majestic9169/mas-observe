import requests, json

schema = {
    "type":"object",
    "properties": {
        "action": {
            "enum":["read_file","write_file","finish"]
        },
        "args":{"type":"object"}
    },
    "required":["action","args"]
}

result = {'action': 'read_file', 'args': {'filename': 'ticket_1.txt'}}

def test_model(model_name):
    r = requests.post(
        "http://127.0.0.1:11434/api/chat", 
        json={
            "model":model_name,
            "stream":False,
            "format":schema,
            "options":{"temperature":0,"num_ctx":4096,"seed":1},
            "messages":[{"role":"user","content":"Read ticket_1.txt"}]
        })
    return json.loads(r.json()["message"]["content"])


print("qwen2.5 20 run test:")
for i in range(20):
    res = test_model("qwen2.5:latest") 
    if res != result:
        print(f"{i}: [err]: model fails harness test")
        print(f"{res}")
        print(f"{result}")
    else:
        print(f"{i}: response matches result")


print("nemotron-3-nano 20 run test:")
for i in range(20):
    res = test_model("nemotron-3-nano:4b") 
    if res != result:
        print(f"{i}: [err]: model fails harness test")
        print(f"{res}")
        print(f"{result}")
    else:
        print(f"{i}: response matches result")
