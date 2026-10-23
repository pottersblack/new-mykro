import json, sys
lines = open('tools/gui_debug.log', encoding='utf-8').readlines()

mode = sys.argv[1] if len(sys.argv) > 1 else 'last'

if mode == 'summary':
    for line in lines[-15:]:
        entry = json.loads(line)
        texts = [e['text'] for e in entry['elements'] if len(e['text']) > 2 and e['conf'] > 60]
        print("seq=%3d | %s" % (entry['seq'], ' '.join(texts[:20])))
else:
    last = json.loads(lines[-1])
    print("seq=%d, total elements=%d, window_rect=%s" % (last['seq'], len(last['elements']), last['window_rect']))
    print("--- All elements by vertical position ---")
    for e in sorted(last['elements'], key=lambda x: x['screen_top']):
        print("  y=%4d x=%5d [%-6s] %s" % (e['screen_top'], e['screen_left'], e['kind'], repr(e['text'])))
