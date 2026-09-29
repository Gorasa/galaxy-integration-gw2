# SPDX-License-Identifier: MIT

"""
Regenerates gw2/db/achievements.json (achievement id -> name) from the Guild Wars 2 API.

The plugin requests names of achievements missing in this file from the API at runtime,
an up to date file only reduces the number of these requests.

Usage: python tools/update_achievements_db.py
"""

import json
import os
import time
import urllib.request

API_URL = 'https://api.guildwars2.com/v2/achievements'
IDS_PER_REQUEST = 200
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'gw2', 'db', 'achievements.json')


def get_json(url):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            print('request failed (%s), retrying: %s' % (e, url))
            time.sleep(2 * (attempt + 1))
    raise RuntimeError('failed to request %s' % url)


def main():
    ids = get_json(API_URL)
    print('found %s achievements' % len(ids))

    names = dict()
    for i in range(0, len(ids), IDS_PER_REQUEST):
        chunk = ids[i:i + IDS_PER_REQUEST]
        for achievement in get_json('%s?ids=%s' % (API_URL, ','.join(str(x) for x in chunk))):
            names[achievement['id']] = achievement['name']

    #do not replace the DB with an incomplete one if the API returned partial data
    if len(names) < len(ids) * 0.9:
        raise RuntimeError('received only %s names for %s achievements' % (len(names), len(ids)))

    with open(DB_PATH, mode='w', encoding='utf-8', newline='\n') as f:
        json.dump({str(k): names[k] for k in sorted(names)}, f, ensure_ascii=False, indent=4)
        f.write('\n')

    print('written %s achievements to %s' % (len(names), os.path.normpath(DB_PATH)))


if __name__ == '__main__':
    main()
