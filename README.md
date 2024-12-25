# for local testing

1. run: python server.py
2. open any api client like rapid API client
3. paste the /make-a-call endpoint in POST request and pass this body in the same:

```
{
  "phone": 9999999999,
  "name": "Ayam",
  "id": "66796c746e6b34001c4480df"
}
```

# for deployment testing

1. paste this endpoint: "https://phoneagent.hyrgpt.com/make-a-call" in postman with POST request
2. use the same sample data as in local testing for the deployment code.

## Imp points

1. https://phoneagent.hyrgpt.com is our dev public deployment URL
2. All API Keys are available in the config file
3. DO NOT CHANGE THE PORT ON WHICH THE APP IS RUNNING