import React from 'react';
import { createRoot } from 'react-dom/client';
import { TurnExecution } from '../src/components/TurnExecution';
import '../src/styles.css';
const details = {
  goals: [
    {id:'goal_1',query:'推荐 500 元以内的鼠标',disposition:'tool_planning',task_ids:['task_1']},
    {id:'goal_2',query:'查询最近一笔订单',disposition:'tool_planning',task_ids:['task_2']}
  ],
  tool_calls: [
    {id:'c1',task_id:'task_1',tool_name:'catalog_search',query:'推荐 500 元以内的鼠标',outcome:'usable',reason:'exact_match',used_in_answer:true,reused:false,superseded:false,arguments:{query:'推荐 500 元以内的鼠标',limit:3},result_summary:{returned_count:3,total_match_count:32,match_kind:'exact'}},
    {id:'c2',task_id:'task_2',tool_name:'order_lookup',query:'查询最近一笔订单',outcome:'error',reason:'timeout',used_in_answer:false,reused:false,superseded:false,arguments:{query:'查询最近一笔订单',limit:1},result_summary:{}}
  ],
  model_calls:{route_request:1,submit_execution_plan:1,tool_planning:1,answer:1}
};
const evidence = [
  {source_type:'knowledge_document' as const,source_id:1,title:'售后政策（模拟资料）',document_type:'policy',snippet:'此处展示本店知识库返回的资料摘要。',metadata:{}},
  {source_type:'web_page' as const,source_id:2,title:'鼠标选购说明（模拟网页）',document_type:'peripheral_knowledge',snippet:'此处展示网页搜索返回的资料摘要。',url:'https://example.com/',metadata:{}}
];
createRoot(document.getElementById('root')!).render(<main style={{maxWidth:360,margin:'32px auto',padding:12}}>
  <h2 style={{fontSize:16}}>聊天记录 · 组件预览</h2><p style={{fontSize:12}}>模拟数据，仅检查布局与折叠交互</p>
  <div className="turn-list"><article className="turn-card"><header><strong>#2</strong><span>部分完成</span></header><p>推荐鼠标，再查一下最近订单</p><div className="turn-facts"><span>商品 3</span><span>订单 0</span></div><TurnExecution details={details} evidence={evidence}/></article>
  <article className="turn-card"><header><strong>#1</strong></header><p>历史请求</p><TurnExecution/></article></div>
</main>);
