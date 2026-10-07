const assert = require('node:assert/strict');
const { buildPolicyClauseTree } = require('../policy/web_ui/clause_tree.js');
const tree = buildPolicyClauseTree([
  {clause_id:'child',parent_clause_id:'parent',sequence_no:3},
  {clause_id:'chapter',sequence_no:1},
  {clause_id:'parent',parent_clause_id:'chapter',sequence_no:2},
  {clause_id:'sibling',parent_clause_id:'chapter',sequence_no:4},
]);
assert.equal(tree.length,1);
assert.deepEqual(tree[0].children.map(n=>n.item.clause_id),['parent','sibling']);
assert.equal(tree[0].children[0].children[0].item.clause_id,'child');
const broken = buildPolicyClauseTree([
  {clause_id:'a',parent_clause_id:'b'}, {clause_id:'b',parent_clause_id:'a'},
  {clause_id:'missing',parent_clause_id:'absent'},
]);
assert.equal(broken.length,3);
console.log('条款树：父子嵌套、同级顺序、缺失父节点和循环关系检查通过');
const labels = buildPolicyClauseTree([
  {clause_id:'chapter',level:'chapter',chapter_path:['第二章']},
  {clause_id:'article',parent_clause_id:'chapter',level:'article',article_no:'八'},
  {clause_id:'item',parent_clause_id:'article',level:'item',article_no:'八',item_no:'（三）'},
  {clause_id:'subitem',parent_clause_id:'item',level:'item',article_no:'八',item_no:'1.'},
]);
assert.deepEqual(labels[0].children[0].children[0].children[0].path,['第二章','八','（三）','1.']);
