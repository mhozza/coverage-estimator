#! /usr/bin/env python
import pystache


def lines_to_list(header, lines):
    res = list()
    for l in lines:
        res.append(
            [l.get(k, None) for k in header]
        )
    return res


def format_table(header, titles, lines, template_file, escape=None, round_floats=4, list=False):
    def format(val):
        if round_floats and type(val) is float:
            val = round(val, round_floats)
        if escape:
            try:
                return escape(val)
            except:
                pass
        return val
    if not list:
        lines = lines_to_list(header, lines)
    data = {
        'header': [
            {'value': format(titles.get(h, h)), 'first': i == 0, 'last': i == len(header) - 1}
            for i, h in enumerate(header)
        ],
        'body': [
            {'line': [
                {'value': format(v), 'first': i == 0, 'last': i == len(l) - 1}
                for i, v in enumerate(l)
            ]} for l in lines
        ],
    }

    with open(template_file) as f:
        template = f.read()
        return pystache.render(template, data)


def square_table(x, y, data):
    x_vals = list(set(v[x] for v in data.values()))
    y_vals = set(v[y] for v in data.values())
    table = [[''] + x_vals]
    for yv in y_vals:
        line = [yv]
        for xv in x_vals:
            for v in data.values():
                if v[x] == xv and v[y] == yv:
                    line.append(v)
                    break
        table.append(line)
    return x_vals, table
