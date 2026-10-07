function buildPolicyClauseTree(clauses) {
  const ordered = [...clauses].sort((a,b)=>(a.sequence_no||0)-(b.sequence_no||0));
  const nodes = new Map(ordered.map(item=>[item.clause_id,{item,children:[]} ]));
  const roots = [];
  for (const item of ordered) {
    const node = nodes.get(item.clause_id);
    const parent = nodes.get(item.parent_clause_id);
    const seen = new Set([item.clause_id]);
    let ancestor = parent, cycle = false;
    // 异常父指针降级为独立条款，避免循环导致丢失内容。
    while (ancestor) {
      if (seen.has(ancestor.item.clause_id)) { cycle=true; break; }
      seen.add(ancestor.item.clause_id);
      ancestor=nodes.get(ancestor.item.parent_clause_id);
    }
    if (parent && !cycle) parent.children.push(node);
    else roots.push(node);
  }
  function assignPath(node, parentPath) {
    const item=node.item;
    const ownLabel = ['book','chapter','section'].includes(item.level)
      ? (item.chapter_path||[]).at(-1)
      : item.level==='article' ? item.article_no
      : item.level==='paragraph' ? item.paragraph_no : item.item_no;
    // 子节点继承实际祖先路径，避免遗漏父项或重复继承条号。
    node.path = parentPath.length ? [...parentPath] : [...(item.chapter_path||[])];
    if (!parentPath.length && !['book','chapter','section'].includes(item.level)) {
      if(item.article_no && item.level!=='article') node.path.push(item.article_no);
      if(item.paragraph_no && item.level!=='paragraph') node.path.push(item.paragraph_no);
    }
    if(ownLabel && node.path.at(-1)!==ownLabel) node.path.push(ownLabel);
    node.children.forEach(child=>assignPath(child,node.path));
  }
  roots.forEach(node=>assignPath(node,[]));
  return roots;
}
if (typeof module !== 'undefined') module.exports = {buildPolicyClauseTree};
