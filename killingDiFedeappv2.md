Here's the command to kill all processes related to DiFedeAppV2:
ps aux | grep -i 'DiFedeAppV2' | grep -v grep | awk '{print $2}' | xargs -r kill -9
That will:
Find all processes with "DiFedeAppV2" in the command line
Filter out the grep process itself
Extract the PIDs
Force kill them all
If you also want to make sure port 5002 is completely clear afterward, you can chain it:
ps aux | grep -i 'DiFedeAppV2' | grep -v grep | awk '{print $2}' | xargs -r kill -9; sleep 1; lsof -i :5002 2>/dev/null | grep LISTEN | awk '{print $2}' | xargs -r kill -9
That second part catches any orphan processes that might be bound to port 5002 but not named with "DiFedeAppV2" in their command (like orphaned eventlet workers).


Ctrl+K to generate command
I added the two files to the linux folder so can you review the model and API key and adjust the MD to reflect how we can use it and what is in place already with anthropic? This system is on my local machine so the api keys will be secure, aside from you seeing them, but whatevs

The white background of the full screen table along with the white text when the column is highlighted makes it where I cannot see what I type into the box. Can you please fix that and review the other color schemes to make sure that we are not doing that anywhere else too?

The previous and next buttons in this look in the second image looks like they are inactive. Please fix that. I assume it is just the text but, I want to make sur there is no DIV stacking issues or anything like that...z index, etc... But If you looke 

Seems like a lot of the themes are very off. Please make sure that we have a text layout on our themes that we are able to see what is being typed. 

cd /home/joe/DiFedeAppV2 && python3 run.py
received event "join_game" from Dj4jNO50PWjqhefAAAAB [/]
INFO:socketio.server:received event "join_game" from Dj4jNO50PWjqhefAAAAB [/]
Dj4jNO50PWjqhefAAAAB is entering room game_369 [/]
INFO:socketio.server:Dj4jNO50PWjqhefAAAAB is entering room game_369 [/]
Server is up and already receiving connections. Let me verify it's responding:
curl -s -o /dev/null -w "%{http_code}" http://localhost:5002/
200
lsof -i :5002 2>/dev/null
python3 3092813  joe    7u  IPv4 44638511      0t0  TCP localhost:5002->localhost:34152 (ESTABLISHED)
python3 3092813  joe    8u  IPv4 44602274      0t0  TCP localhost:5002->localhost:60886 (ESTABLISHED)
python3 3092813  joe    9u  IPv4 44647440      0t0  TCP localhost:5002->localhost:53610 (ESTABLISHED)
python3 3098310  joe    3u  IPv4 44621205      0t0  TCP *:5002 (LISTEN)
Two processes listening on 5002 -- that's the problem. Stale process is serving old code.
kill -9 3092813 3098310 2>/dev/null; sleep 1; lsof -i :5002 2>/dev/null || echo "Port 5002 clear"