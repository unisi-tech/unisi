"use strict";(globalThis["webpackChunkuniqua"]=globalThis["webpackChunkuniqua"]||[]).push([[260],{4260:(t,e,a)=>{a.r(e),a.d(e,{default:()=>m});var i=a(1758),s=a(8790);function n(t,e,a,n,o,l){return(0,i.uX)(),(0,i.CE)("div",{style:(0,s.Tr)(a.styleSize?a.styleSize:l.currentStyle())},[((0,i.uX)(),(0,i.Wv)((0,i.$y)(o.VChartAsync),{ref:"chart","manual-update":!0,onClick:l.clicked,autoresize:!0},null,8,["onClick"]))],4)}a(8499);var o=a(4907),l=a(3952),r=a(8734);let h=["","#80FFA5","#00DDFF","#37A2FF","#FF0087","#FFBF00","rgba(128, 255, 165)","rgba(77, 119, 255)"];const d={name:"linechart",props:{data:Object,pdata:Object,styleSize:String},data(){const t=(0,o.A)();return{$q:t,model:!1,animation:null,markPoint:null,options:{responsive:!0,maintainAspectRatio:!1,legend:{data:[],bottom:10,textStyle:{color:"#4DD0E1"}},tooltip:{trigger:"axis",position:function(t){return[t[0],"10%"]}},title:{left:"center",text:""},toolbox:{feature:{}},xAxis:{type:"category",boundaryGap:!1,data:null},yAxis:{type:"value",boundaryGap:[0,"100%"]},dataZoom:[{type:"inside",start:0,end:10},{start:0,end:10}],series:[]},VChartAsync:null}},computed:{fullname(){return`${this.data.name}@${this.pdata.name}`}},methods:{async loadChartComponents(){const[{default:t},e]=await Promise.all([Promise.all([a.e(121),a.e(464)]).then(a.bind(a,6581)),Promise.all([a.e(121),a.e(464)]).then(a.bind(a,1879))]);this.VChartAsync=(0,r.IG)(t),this.echarts=e},setOptions(){this.$refs.chart&&this.$refs.chart.setOption(this.options)},currentStyle(){let t=this.data.tablerect,e=t?t.width:300,a=t?t.height:200;return`width: ${e}px; height: ${a}px`},processCoord(t,e,a){let i=null;for(let a of e)if(t[0]==a.coord[0]){i=a;break}a?i?e.splice(e.indexOf(i),1):e.push({coord:t}):(e.splice(0,e.length),i||e.push({coord:t}))},clicked(t){let e=[t.dataIndex,this.options.series[t.seriesIndex].data[t.dataIndex]];this.processCoord(e,this.markPoint.data,l.Zp.shiftKey);let a=this.markPoint.data.map((t=>t.coord[0])),i=this.data;if(i.value=Array.isArray(i.value)||a.length>1?a:a.length?a[0]:null,this.animation){let t=this.options.dataZoom;t[0].start=this.animation.start,t[1].start=this.animation.start,t[0].end=this.animation.end,t[1].end=this.animation.end}this.setOptions(),(0,l.tN)([this.pdata.name,this.data.name,"changed",this.data.value])},calcSeries(){this.options.toolbox.feature.mySwitcher={show:!0,title:"Switch view to the table",icon:"image:M0 2a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V2zm15 2h-4v3h4V4zm0 4h-4v3h4V8zm0 4h-4v3h3a1 1 0 0 0 1-1v-2zm-5 3v-3H6v3h4zm-5 0v-3H1v2a1 1 0 0 0 1 1h3zm-4-4h4V8H1v3zm0-4h4V4H1v3zm5-3v3h4V4H6zm4 4H6v3h4V8z",onclick:()=>{let t=this.data;t.type="table",t.tablerect=this.$refs.chart.$el.getBoundingClientRect(),l.Yt[this.fullname].styleSize=this.currentStyle()}};let t=this.data.view,e=this.data.headers;"_"!=this.data.name[0]&&(this.options.title.text=this.data.name);let a=t.split("-"),i=a[1].split(",");i.unshift(a[0]);let s=[];for(let t=0;t<i.length;t++)i[t]="i"==i[t]?-1:parseInt(i[t]),s.push([]),t&&(this.options.series.push({name:e[i[t]],type:"line",symbol:"circle",symbolSize:10,sampling:"lttb",itemStyle:{color:h[t]},data:s[t]}),this.options.legend.data.push(e[i[t]]));this.options.xAxis.data=s[0];let n=this.data.rows;for(let t=0;t<n.length;t++)for(let e=0;e<i.length;e++)s[e].push(-1==i[e]?t:n[t][i[e]]);if(this.options.series[1]){let t=[],e=this.options.series[1].data,a=Array.isArray(this.data.value)?this.data.value:null===this.data.value?[]:[this.data.value];for(let i=0;i<a.length;i++)this.processCoord([a[i],e[a[i]]],t,!0);this.markPoint={symbol:"rect",symbolSize:10,animationDuration:300,silent:!0,label:{color:"#fff"},itemStyle:{color:"blue"},data:t},this.options.series[1].markPoint=this.markPoint}this.setOptions()}},async mounted(){await this.loadChartComponents(),this.calcSeries();let t=this;this.$refs.chart.chart.on("datazoom",(function(e){(e.start||e.end)&&(t.animation=e)}))}};var c=a(2807);const u=(0,c.A)(d,[["render",n]]),m=u}}]);