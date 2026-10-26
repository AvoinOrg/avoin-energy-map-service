"""Read the dedicated sandbox administrator in memory, never an application owner."""
import re

from .private import read, require

ADMIN_NOTE = '/home/xbexbex/syncthing/work/avoin/projects/avoin-infra-stuff/gis/secrets.md'


def sandbox_admin_password(content, expected_user):
    lines=content.splitlines()
    nodes=[]
    stack=[]
    for number,line in enumerate(lines):
        match=re.match(r'^(\s*)[-*]\s+([^:]+?)\s*$',line)
        if not match:continue
        depth=len(match[1].expandtabs(4))
        while stack and nodes[stack[-1]]['depth']>=depth:stack.pop()
        nodes.append(dict(start=number,depth=depth,title=match[2].strip().casefold(),parent=stack[-1] if stack else None))
        stack.append(len(nodes)-1)
    def fields(index):
        node=nodes[index]
        end=next((n['start'] for n in nodes[index+1:] if n['depth']<=node['depth']),len(lines))
        result={}
        for line in lines[node['start']+1:end]:
            match=re.match(r'^\s*[-*]\s+([^:]+):\s*(.+)$',line)
            if match:result.setdefault(match[1].strip().casefold(),[]).append(match[2].strip().strip('`'))
        return result
    groups=[i for i,node in enumerate(nodes) if 'sandbox' in node['title'] and 'postgis' in node['title'] and
        any(n['parent']==i and 'admin' in n['title'] and 'gis' in n['title'] for n in nodes)]
    require(len(groups)==1,'admin_reference')
    candidates=[i for i,node in enumerate(nodes) if node['parent']==groups[0] and 'postgres' in node['title'] and
        fields(i).get('database') and any('user' in k for k in fields(i)) and any('password' in k for k in fields(i))]
    require(len(candidates)==1,'admin_reference')
    selected=fields(candidates[0])
    users=[value for key,values in selected.items() if 'user' in key for value in values]
    passwords=[value for key,values in selected.items() if 'password' in key for value in values]
    require(users==[expected_user] and len(passwords)==1 and passwords[0],'admin_reference')
    return passwords[0]


def admin_password(target):
    if target['mode']=='sandbox':
        require(target['admin_password_file']==ADMIN_NOTE,'admin_reference')
        return sandbox_admin_password(read(ADMIN_NOTE),target['admin_user'])
    return read(target['admin_password_file']).strip()
