"""Client <-> server WebSocket protocol (reference).

Client connects to  ws://<host>:<port>/ws  and authenticates:

  -> {"op":"auth","token":"<token from /api/login or /api/register>"}
  <- {"op":"auth_ok","user":{...},"sessions":[...],"friends":[...],
      "requests":[...],"server":{...}}

Requests (client -> server), all answered with op:"result":

  {"op":"message","req_id":1,"session_id":"abc","message":"hi" | [segments]}
  {"op":"create_session","req_id":2,"kind":"ai","name":"新对话"}
  {"op":"history","req_id":3,"session_id":"abc","before_id":null,"limit":50}
  {"op":"friend_add","req_id":4,"username":"alice"}
  {"op":"friend_requests","req_id":5}
  {"op":"friend_handle","req_id":6,"user_id":2,"approve":true}
  {"op":"friend_delete","req_id":7,"user_id":2}
  {"op":"ping","req_id":8}

Responses:
  {"op":"result","req_id":1,"status":"ok","data":{...}}
  {"op":"result","req_id":1,"status":"failed","msg":"..."}

Server push (OneBot V11 style events + KiteChat notices):

  {"post_type":"message","message_type":"private","session_id":"abc",
   "message_id":12,"sender":{"user_id":0,"nickname":"Kite AI"},
   "message":[segments],"raw_message":"...","time":1700000000}

  {"post_type":"notice","notice_type":"bot_typing","session_id":"abc","typing":true}
  {"post_type":"notice","notice_type":"friend_added","user_id":2,...}
  {"post_type":"notice","notice_type":"friend_removed", ...}
  {"post_type":"notice","notice_type":"friend_rejected", ...}
  {"post_type":"notice","notice_type":"session_created","session":{...}}
  {"post_type":"request","request_type":"friend","user_id":2,"comment":"..."}
  {"post_type":"meta_event","meta_event_type":"presence","user_id":2,"online":true}
  {"post_type":"meta_event","meta_event_type":"bridge","connected":true}

Bot/OneBot side (reverse WS): bots connect to ws://<host>:<port>/onebot and
exchange standard OneBot V11 events/APIs. Each user has a virtual number
(users.virtual_qq) assigned starting from #1.
"""
