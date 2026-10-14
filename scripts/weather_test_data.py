import datetime
import json

NOW = datetime.datetime(2026, 10, 15, 0, 45, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
OBSERVATION = {'city': '101130112', 'cityname': '米东', 'temp': '8.5', 'SD': '56%',
               'weather': '多云', 'weathercode': 'd01', 'WD': '东南风', 'WS': '1级',
               'wse': '3km/h', 'time': '00:40', 'date': '10月15日(星期四)'}
FORECAST = {'f0': '20261014180000', 'f': [
    {'fi': '10/14', 'fc': '21', 'fd': '3', 'fa': '01', 'fm': '70', 'fn': '60.3'},
    {'fi': '10/15', 'fc': '16', 'fd': '-1', 'fa': '07', 'fm': '95.5', 'fn': '52.5'},
    {'fi': '10/16', 'fc': '14', 'fd': '-4', 'fa': '07'}]}


def search_row(identity='101130112', title='米东', province='新疆'):
    return {'ref': '~'.join([identity, 'pinyin', title, 'Pinyin', title, 'Pinyin', '0991', '830000', 'PY', province])}


def search_body(rows):
    return ('(' + json.dumps(rows, ensure_ascii=False) + ')').encode()


def assignment(name, value):
    return ('var ' + name + '=' + json.dumps(value, ensure_ascii=False) + ';').encode()
