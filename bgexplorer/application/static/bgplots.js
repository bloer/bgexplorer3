/* Interactive plots for Background Explorer, drawn with plotly.js from the
 * JSON produced by application/plotting.py
 */
const bgplots = (function(){
    "use strict";

    const COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
                    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'];
    const MEASURED_COLOR = '#d62728';
    const LIMIT_COLOR = '#1f4fd6';
    const CONFIG = {responsive: true, displaylogo: false};

    function el(tag, attrs, children){
        const node = document.createElement(tag);
        for(const [key, value] of Object.entries(attrs || {})){
            if(key === 'text')
                node.textContent = value;
            else
                node.setAttribute(key, value);
        }
        (children || []).forEach(child => node.appendChild(child));
        return node;
    }

    function getJSON(url){
        return fetch(url).then(response => response.json().then(data => {
            if(!response.ok)
                throw new Error((data.error && data.error.message) || response.statusText);
            return data;
        }));
    }

    function showError(div, error){
        if(window.Plotly)
            Plotly.purge(div);
        div.replaceChildren(el('div', {'class': 'alert alert-danger',
                                       text: String(error.message || error)}));
    }

    /* Run `load` the first time `div` is visible, e.g. in a hidden tab */
    function whenVisible(div, load){
        if(!('IntersectionObserver' in window)){
            load();
            return;
        }
        const observer = new IntersectionObserver(entries => {
            if(entries.some(entry => entry.isIntersecting)){
                observer.disconnect();
                load();
            }
        });
        observer.observe(div);
    }

    function toggle(id, label, checked){
        const input = el('input', {'class': 'form-check-input', type: 'checkbox', id: id});
        input.checked = checked;
        return [input, el('div', {'class': 'form-check form-check-inline'},
                          [input, el('label', {'class': 'form-check-label', 'for': id, text: label})])];
    }

    function unitLabel(units){
        return units ? ` [${units}]` : '';
    }

    /* Step-line traces with a shaded error band for one histogram */
    function spectrumTraces(name, h, color, logy){
        // repeat the last value so the final bin is drawn to its high edge
        const x = h.bins;
        const pad = arr => arr.concat(arr.length ? [arr[arr.length-1]] : []);
        const y = pad(h.value);
        const smallest = Math.min(...h.value.filter(v => v > 0));
        const floor = (logy && isFinite(smallest)) ? smallest / 100 : null;
        const low = pad(h.value.map((v, i) => {
            const lo = v - h.err_minus[i];
            return (logy && !(lo > 0)) ? floor : lo;
        }));
        const high = pad(h.value.map((v, i) => v + h.err_plus[i]));
        const common = {x: x, mode: 'lines', line: {shape: 'hv', width: 0},
                        legendgroup: name, showlegend: false, hoverinfo: 'skip'};
        return [
            Object.assign({}, common, {y: low}),
            Object.assign({}, common, {y: high, fill: 'tonexty',
                                       fillcolor: color + '40'}),
            {x: x, y: y, name: name, legendgroup: name, mode: 'lines',
             line: {shape: 'hv', color: color, width: 1.5}},
        ];
    }

    /* Plot the spectra served as {name: histogram_json} by `url` into
     * `div`, with a selector for which spectra to show and log-scale
     * toggles. Data are fetched when the div first becomes visible.
     */
    function spectrum(div, url){
        if(typeof div === 'string')
            div = document.getElementById(div);
        whenVisible(div, () => {
            div.replaceChildren(el('div', {'class': 'spinner-border', role: 'status'}));
            getJSON(url).then(data => drawSpectra(div, data))
                        .catch(error => showError(div, error));
        });
    }

    function drawSpectra(div, data){
        const names = Object.keys(data);
        if(!names.length){
            div.replaceChildren(el('p', {'class': 'text-secondary', text: 'No spectra'}));
            return;
        }
        const id = div.id || 'spectrum';
        const select = el('select', {'class': 'form-select form-select-sm w-auto',
                                     multiple: '', size: String(Math.min(names.length, 4)),
                                     'aria-label': 'spectra to show'},
                          names.map(name => el('option', {value: name, text: name})));
        select.options[0].selected = true;
        const [logx, logxdiv] = toggle(id + '_logx', 'log x', false);
        const [logy, logydiv] = toggle(id + '_logy', 'log y', true);
        const plot = el('div', {'class': 'bgplot'});
        const controls = el('div', {'class': 'd-flex gap-3 align-items-center mb-2'},
                            [select, logxdiv, logydiv]);
        div.replaceChildren(controls, plot);
        div.plot = plot;

        function draw(){
            const selected = Array.from(select.selectedOptions).map(o => o.value);
            const traces = [];
            selected.forEach((name, i) => traces.push(
                ...spectrumTraces(name, data[name], COLORS[i % COLORS.length], logy.checked)));
            const first = data[selected[0]] || {};
            const layout = {
                margin: {t: 20, r: 10},
                xaxis: {title: {text: 'Energy' + unitLabel(first.binsunit)},
                        type: logx.checked ? 'log' : 'linear'},
                yaxis: {title: {text: unitLabel(first.units).trim()},
                        type: logy.checked ? 'log' : 'linear', exponentformat: 'power'},
                showlegend: selected.length > 1,
            };
            Plotly.react(plot, traces, layout, CONFIG);
        }
        [select, logx, logy].forEach(input => input.addEventListener('change', draw));
        draw();
    }

    /* A horizontal budget chart: measured values as red points with error
     * bars, upper limits as blue left-pointing arrows from the 90% limit
     */
    function budgetTraces(rows){
        const measured = rows.filter(r => r.measured && r.measured.value > 0);
        const limits = rows.filter(r => r.limit && r.limit.upper_limit > 0);
        const arrowx = [], arrowy = [];
        limits.forEach(r => {
            arrowx.push(r.limit.upper_limit, r.limit.upper_limit / 4, null);
            arrowy.push(r.label, r.label, null);
        });
        return [
            {type: 'scatter', mode: 'lines', x: arrowx, y: arrowy,
             line: {color: LIMIT_COLOR, width: 2}, hoverinfo: 'skip',
             showlegend: false, legendgroup: 'limit'},
            {type: 'scatter', mode: 'markers', name: '90% upper limit',
             legendgroup: 'limit',
             x: limits.map(r => r.limit.upper_limit / 4),
             y: limits.map(r => r.label),
             customdata: limits.map(r => r.limit.upper_limit),
             hovertemplate: '%{y}: < %{customdata:.3g}<extra></extra>',
             marker: {symbol: 'triangle-left', size: 10, color: LIMIT_COLOR}},
            {type: 'scatter', mode: 'markers', name: 'Measured',
             x: measured.map(r => r.measured.value),
             y: measured.map(r => r.label),
             error_x: {type: 'data', symmetric: false,
                       array: measured.map(r => r.measured.err_plus),
                       // keep the lower bar on a log axis
                       arrayminus: measured.map(r => Math.min(r.measured.err_minus,
                                                              0.999 * r.measured.value)),
                       color: MEASURED_COLOR},
             hovertemplate: '%{y}: %{x:.3g}<extra></extra>',
             marker: {symbol: 'circle', size: 8, color: MEASURED_COLOR}},
        ];
    }

    function rowSize(row){
        return Math.max(row.measured ? row.measured.value : 0,
                        row.limit ? row.limit.upper_limit : 0);
    }

    function drawBudget(plot, data, threshold, title){
        const total = data.rows.reduce((sum, r) => sum + rowSize(r), 0);
        const rows = data.rows.filter(r => rowSize(r) > 0
                                      && rowSize(r) >= threshold * total);
        // clear plotly's state along with any message
        Plotly.purge(plot);
        plot.replaceChildren();
        if(!rows.length){
            plot.appendChild(el('p', {'class': 'text-secondary', text: 'No contributions'}));
            return;
        }
        const layout = {
            title: {text: title, font: {size: 14}},
            margin: {t: 30, r: 10, l: 10},
            height: 120 + 22 * rows.length,
            xaxis: {type: 'log', title: {text: data.scalar + unitLabel(data.units)},
                    exponentformat: 'power', gridcolor: '#ddd'},
            yaxis: {type: 'category', automargin: true, autorange: 'reversed',
                    categoryorder: 'array', categoryarray: rows.map(r => r.label)},
            legend: {orientation: 'h', y: -0.15},
            showlegend: false,
        };
        Plotly.react(plot, budgetTraces(rows), layout, CONFIG);
    }

    /* Budget breakdowns of one component, one panel per groupby. `url` is
     * the budget.json endpoint, `scalars` the result names to choose from
     */
    function budget(div, url, groupbys, scalars){
        if(typeof div === 'string')
            div = document.getElementById(div);
        const id = div.id || 'budget';
        const select = el('select', {'class': 'form-select form-select-sm w-auto',
                                     id: id + '_scalar', 'aria-label': 'result to plot'},
                          scalars.map(name => el('option', {value: name, text: name})));
        const threshold = el('input', {'class': 'form-control form-control-sm', type: 'number',
                                       min: '0', max: '100', step: 'any', value: '0',
                                       id: id + '_threshold', style: 'width: 6em'});
        const controls = el('div', {'class': 'd-flex gap-2 align-items-center mb-2'}, [
            el('label', {'for': select.id, text: 'Result'}), select,
            el('label', {'class': 'ms-3', 'for': threshold.id, text: 'Hide below'}), threshold,
            el('span', {text: '% of total'})]);
        const panels = groupbys.map(groupby => el('div', {'class': 'col-lg-4 bgplot',
                                                          'data-groupby': groupby}));
        div.replaceChildren(controls, el('div', {'class': 'row'}, panels));
        const cache = {};

        function draw(){
            const scalar = select.value;
            panels.forEach(panel => {
                const groupby = panel.dataset.groupby;
                const key = scalar + '|' + groupby;
                const sep = url.includes('?') ? '&' : '?';
                const params = new URLSearchParams({scalar: scalar, groupby: groupby});
                cache[key] = cache[key] || getJSON(url + sep + params);
                cache[key].then(data => drawBudget(panel, data, threshold.value / 100,
                                                   'By ' + groupby))
                          .catch(error => showError(panel, error));
            });
        }
        select.addEventListener('change', draw);
        threshold.addEventListener('change', draw);
        whenVisible(div, draw);
    }

    return {spectrum: spectrum, budget: budget};
})();
