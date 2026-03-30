import './cookies-policy';
import 'htmx.org/dist/htmx.min.js';
import '../public/js/app.js';
import './js/components/project-data-table.js';
import './js/components/files-data-table.js';
import './js/components/file-upload.js';
import './js/components/notify.js';
import './js/code-blocks.js';

import Alpine from 'alpinejs';
import Chart from 'chart.js/auto';
import Swiper from 'swiper/bundle';
import 'swiper/css/bundle';
import SimpleBar from 'simplebar';
import 'simplebar/dist/simplebar.css';
import 'iconify-icon';

window.Alpine = Alpine;
window.Chart = Chart;
window.Swiper = Swiper;
window.SimpleBar = SimpleBar;

Alpine.start();
