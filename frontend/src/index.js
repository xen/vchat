import './cookies-policy';
import 'htmx.org/dist/htmx.min.js';
import '../public/js/app.js';
import './js/components/project-data-table.js';
import './js/components/project-files-table.js';
import './js/components/document-links-graph.js';
import './js/components/notify.js';
import './js/code-blocks.js';

import Alpine from 'alpinejs';
import Chart from 'chart.js/auto';
import * as d3 from 'd3';
import Swiper from 'swiper/bundle';
import 'swiper/css/bundle';
import SimpleBar from 'simplebar';
import 'simplebar/dist/simplebar.css';
import 'iconify-icon';
import { addCollection } from 'iconify-icon';
import lucideIcons from '@iconify-json/lucide/icons.json';
addCollection(lucideIcons);

window.htmx = window.htmx || globalThis.htmx;
window.Alpine = Alpine;
window.Chart = Chart;
window.d3 = d3;
window.Swiper = Swiper;
window.SimpleBar = SimpleBar;

Alpine.start();
