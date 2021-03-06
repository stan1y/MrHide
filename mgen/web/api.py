"""
MGEN Rest API Inteface classes
"""

import zlib
import uuid
import json
import pprint
import functools
import logging
import traceback
import datetime
import enum

import tornado
import tornado.web
import tornado.template

import sqlalchemy
import sqlalchemy.exc

import mgen.util
import mgen.error
import mgen.model

from mgen.model import session, get_primary_key
from mgen.model import Permission

from mgen.web import BaseRequestHandler
from mgen.web.auth import authenticated


log = logging.getLogger(__name__)


def jsonify(method):
    """Decorate methods with this to output valid JSON data."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        answer = method(self, *args, **kwargs)
        if answer:
            if self._finished:
                log.warn('trying to write JSON on finished request.')
            else:
                self.set_header('Content-Type', 'application/json')
                self.write(json.dumps(answer, cls=mgen.util.JSONEncoder))
    return wrapper


class BaseAPIRequestHandler(BaseRequestHandler):
    """Base class for API requests processing"""
    
    def write_error(self, status_code, **kwargs):
        '''Override default error rendering to report JSON for API calls'''
        ex_type, ex, ex_trsbk = kwargs['exc_info']
        if not ex or not isinstance(ex, mgen.error.MGENException):
            return super().write_error(status_code, **kwargs)
                
        ex.append('traceback', traceback.format_exception(ex_type, ex, ex_trsbk))
        self.set_header('Content-Type', 'application/json')
        ex_msg = ex.format()
        log.error('Exception: %s' % ex_msg)
        self.write(ex_msg)
    
    @property
    def request_params(self):
        '''Read and validate client JSON data for current request'''
        
        if hasattr(self, '__cached_request_params'):
            return getattr(self, '__cached_request_params')
        
        try:
            content_type = self.request.headers.get('Content-Type')
            params = {}
            
            body = self.request.body
            log.debug('parsing request params from "%s"' % content_type)
            # handle different type of params input
            
            if 'application/json' in content_type:
                params = json.loads(body.decode('utf-8'))
            
            if 'application/lzg+json' in content_type:
                try:
                    body = zlib.decompress(body)
                except zlib.error as ex:
                    raise mgen.error.BadRequest().describe("can't decompress body. %s" % ex)
                
                params = json.loads(body.decode('utf-8'))
    
            if 'application/x-www-form-urlencoded' in content_type:
                for key in self.request.body_arguments:
                    params[key] = self.get_body_argument(key)
    
            # cache parsed result
            setattr(self, '__cached_request_params', params)
            
            log.debug('-- request params --')
            log.debug(pprint.pformat(params, indent=2, width=160))
            log.debug('-- end request params --')
            
            return getattr(self, '__cached_request_params')
                
        except ValueError:
            raise mgen.error.BadRequest().describe('invalid json received')


def collection_name(model):
    return model.__tablename__ + "s"


class GenericModelHandler(BaseAPIRequestHandler):
    """Generic REST API handler for models access with paging, sorting, filtering, etc"""
    
    @property
    def page_arguments(self):
        """Returns tuple of pageing arguments of sent by client"""
            
        #check that all three fields are present
        if 'page' in self.request.arguments:
            page = int(self.get_argument('page'))
        else:
            return False, 0, 0, 0
        if 'limit' in self.request.arguments:
            limit = int(self.get_argument('limit'))
        else:
            return False, 0, 0, 0
        if 'start' in self.request.arguments:
            start = int(self.get_argument('start'))
        else:
            return False, 0, 0, 0    
        
        return True, page, start, limit
        
    def sort(self, model, query):
        '''Server-side sorting support'''
        if not 'sort' in self.request.arguments:
            return query
        if not issubclass(model, mgen.model.SortModelMixin):
            log.warn('not a sorted instance')
            return query
        
        return model.sort(query, self.get_argument('sort'))
    
    def filter(self, model, query):
        '''Server side filtering support'''
        if not 'filter' in self.request.arguments:
            return query
        if not issubclass(model, mgen.model.FilterModelMixin):
            log.warn('not a sorted instance')
            return query

        return model.filter(query, self.get_argument('filter'))

    def range(self, model, query):
        '''Server side range query support'''
        if not 'range' in self.request.arguments:
            return query
        if not issubclass(model, mgen.model.RangeModelMixin):
            log.warn('not a sorted instance')
            return query
        return model.range(query, self.get_argument('range'))

    def fetch_page(self, query, collection="objects", **kwargs):
        """Generic get method for models with paging support and conversion to json"""
        
        should_page, page_no, start, limit = self.page_arguments
        if should_page and isinstance(query, sqlalchemy.orm.Query):
            query = query.offset(start).limit(limit)
        else:
            log.debug('fetch list - paging disabled by client')

        # perform query and convert results to json
        log.debug('--- Begin SQL PAGE query ---')
        log.debug(str(query))
        log.debug('--- End SQL PAGE query ---')
        
        objects = [obj.to_json() for obj in query]
        return {
            "page": page_no,
            "limit": limit,
            "start": start,
            "total": len(objects),
            collection: objects
        }
        
    def get_objects(self, model, query, primary_key = None):
        '''Modify base query to get page of objects or single one.'''
        cname = collection_name(model)
        if primary_key:
            pkey = get_primary_key(model)
            log.info('select one %s -> %s = "%s"' % (model.__tablename__, pkey, primary_key))
            query = query.filter(pkey == primary_key)
            log.debug('-- Begin SQL GET query --')
            log.debug(str(q))
            log.debug('-- End SQL GET query --')
            obj = query.first()
            if not obj:
                raise mgen.error.NotFound().describe('object with %s="%s" was not found in %s' % (
                    pkey, primary_key, cname))
            return {
                'total': 1,
                cname: [obj]
            }
        else:
            do_page, page, start, limit = self.page_arguments
            qinfo = ''
            if 'filter' in self.request.arguments:
                flt = self.get_argument('filter')
                flt_obj = json.loads(flt)
                qinfo += pprint.pformat(flt_obj, indent=2, width=80)
            else:
                qinfo = 'everything'

            if do_page:
                qinfo += ' page=%d, start=%d, limit=%d' % (page, start, limit)
                
            log.info('select %s -> %s' % (model.__tablename__, qinfo))
            
            query = self.sort(model, query)
            query = self.filter(model, query)
            query = self.range(model, query)
            return self.fetch_page(query, cname)
        
    
    def commit_changes(self, s = None):
        cls_name = self.__class__.__name__
        try:
            # use default session if param was omitted
            if s is None: s = session()
            s.commit()
            s.flush()
            log.debug("commited changes to session %s" % s)
        
        except sqlalchemy.exc.IntegrityError as ie:
            raise mgen.error.Conflict().duplicate_object(
                'Failed to create new object in "{0}". Error: {1}'.format(
                cls_name, ie)
            )
            
        except Exception as ex:
            raise mgen.error.BadRequest().describe(
                'Failed to create new object in "{0}". Error: {1}'.format(
                cls_name, ex)
            )
            
    def validate_params(self, params_list):
        '''Check that given list of param names in present in current request'''
        for pname in params_list:
            not_none = False
            if isinstance(pname, tuple):
                pname, not_none = pname
            if pname not in self.request_params:
                raise mgen.error.BadRequest().describe('missing property "%s"' % pname)
            val = self.request_params[pname]
            if not_none and (val == None or len(val) == 0):
                raise mgen.error.BadRequest().describe('no value given for property "%s"' % pname)


class Profiles(GenericModelHandler):
    """Profiles restfull interface"""
    
    @authenticated
    @jsonify
    def get(self, profile_id=None):
        """GET list or single profile"""
        q = session().query(Profile)
        return self.get_objects(mgen.model.Profile, q, profile_id)


class Projects(GenericModelHandler):
    """"Projects restfull interface"""
    
    def query(self):
        # select project.title, profile.name, project2profile.permission 
        # from project join project2profile on project2profile.project = project.project_id 
        # join profile on profile.email = project2profile.profile 
        # where profile.email = @profile
        return session().query(mgen.model.Project).join(mgen.model.project2profile,
                                             mgen.model.project2profile.c.project==mgen.model.Project.project_id)\
                                       .join(mgen.model.Profile,
                                             mgen.model.Profile.email==mgen.model.project2profile.c.profile)\
                                       .filter(mgen.model.Profile.email==self.current_profile.email)
                                       
    
    @authenticated
    @jsonify
    def get(self, project_id=None):
        """GET list or single project"""
        log.debug('REST GET %s -> %s' % (self.request.path,
            pprint.pformat(self.page_arguments) if project_id is None else '%s=%s' % (
                get_primary_key(mgen.model.Project), project_id) ))
        return self.get_objects(mgen.model.Project, self.query(), project_id)

        
    @authenticated
    @jsonify
    def post(self):
        log.debug("REST POST %s <- %s" % (self.request.path,
                                          pprint.pformat(self.request_params,
                                                         indent=2,
                                                         width=160)))
        """POST to create a new project"""
        self.validate_params([
            'title', 'public_base_uri', 'options'
        ])
        s = session()
        project = mgen.model.Project(project_id=uuid.uuid4(),
                                     title=self.request_params['title'],
                                     public_base_uri=self.request_params['public_base_uri'])
        s.add(project)
        
        perm = mgen.model.ProjectPermission.grant(Permission.all(),
                                                  project.project_id,
                                                  self.current_profile.email)
        s.add(perm)
        
        self.commit_changes(s)
        self.set_status(201)
        
        log.debug('created new project: %s' % project.project_id)
        return self.get_objects(mgen.model.Project,
                                self.query(),
                                project.project_id)


class Pages(GenericModelHandler):
    
    def query(self):
        return session().query(mgen.model.Page).join(mgen.model.Project,
                                                         mgen.model.Project.project_id==mgen.model.Page.project_id)\
                                                   .join(mgen.model.project2profile,
                                                         mgen.model.project2profile.c.project==mgen.model.Project.project_id)\
                                                   .join(mgen.model.Profile,
                                                         mgen.model.Profile.email==mgen.model.project2profile.c.profile)\
                                                   .filter(mgen.model.Profile.email==self.current_profile.email)
    
    @authenticated
    @jsonify
    def get(self, page_id=None):
        log.debug('REST GET %s -> %s' % (self.request.path,
            pprint.pformat(self.page_arguments) if page_id is None else '%s=%s' % (
                get_primary_key(mgen.model.Page), page_id) ))
        return self.get_objects(mgen.model.Page, self.query(), page_id)
        
        
    @authenticated
    @jsonify
    def post(self):
        log.debug("REST POST %s <- %s" % (self.request.path,
                                          pprint.pformat(self.request_params,
                                                         indent=2,
                                                         width=160)))
        self.validate_params([
            ('path', True),
            ('input', True),
            'project_id',
            'template_id'])
            
            
        proj_id = self.request_params['project_id']
        s = session()
        project = s.query(mgen.model.Project).filter_by(project_id=proj_id).one()
        p = project.get_permission(self.current_profile.email)
        if not p & Permission.Edit:
            raise mgen.error.Forbidden().describe("You cannot modify project " + proj_id)
            
        p = mgen.model.Page(page_id=uuid.uuid4(),
                            path=self.request_params['path'],
                            input=json.loads(self.request_params['input']),
                            project_id=self.request_params['project_id'],
                            template_id=self.request_params['template_id'])
                            
        s.add(p)
        self.commit_changes(s)
        self.set_status(201)
        
        log.debug('created new page: %s' % p.page_id)
        return self.get_objects(mgen.model.Template,
                                self.query(),
                                p.page_id)
                            
                                 
class Templates(GenericModelHandler):
    """"Templates restfull interface"""
    
    def query(self):
        # select template.name, project.title, profile.name, project2profile.permission
        # from template join project on project.project_id = template.project_id 
        # join project2profile on project2profile.project = project.project_id
        # join profile on profile.email = project2profile.profile
        # where profile.email = @profile
        return session().query(mgen.model.Template).join(mgen.model.Project,
                                                         mgen.model.Project.project_id==mgen.model.Template.project_id)\
                                                   .join(mgen.model.project2profile,
                                                         mgen.model.project2profile.c.project==mgen.model.Project.project_id)\
                                                   .join(mgen.model.Profile,
                                                         mgen.model.Profile.email==mgen.model.project2profile.c.profile)\
                                                   .filter(mgen.model.Profile.email==self.current_profile.email)

    @authenticated
    @jsonify
    def get(self, template_id=None):
        """GET list or single template"""
        log.debug('REST GET %s -> %s' % (self.request.path,
            pprint.pformat(self.page_arguments) if template_id is None else '%s=%s' % (
                get_primary_key(mgen.model.Template), template_id) ))
        return self.get_objects(mgen.model.Template, self.query(), template_id)

    @authenticated
    @jsonify
    def post(self):
        log.debug("REST POST %s <- %s" % (self.request.path,
                                          pprint.pformat(self.request_params,
                                                         indent=2,
                                                         width=160)))
        self.validate_params([
            ('name', True),
            ('type', True),
            'params',
            'data'])
        
        proj_id = self.request_params['project_id']
        s = session()
        project = s.query(mgen.model.Project).filter_by(project_id=proj_id).one()
        p = project.get_permission(self.current_profile.email)
        if not p & Permission.Edit:
            raise mgen.error.Forbidden().describe("You cannot modify project " + proj_id)
            
        tmpl = mgen.model.Template(template_id=uuid.uuid4(),
                                   name=self.request_params['name'],
                                   type=self.request_params['type'],
                                   project_id=self.request_params['project_id'],
                                   data=self.request_params['data'],
                                   params=self.request_params['params'])
        
        if tmpl.type not in mgen.generator.template.template_types:
            raise mgen.error.BadRequest().describe('unsupported template type: %s. supported: %s' % (
                tmpl.type, ', '.join(mgen.generator.item_types.keys())))
        
        s.add(tmpl)
        self.commit_changes(s)
        self.set_status(201)
        
        log.debug('created new template: %s' % tmpl.template_id)
        return self.get_objects(mgen.model.Template,
                                self.query(),
                                tmpl.template_id)

    @authenticated
    @jsonify
    def put(self, template_id):
        self.validate_params([
            ('pk', True),
            ('name', True),
            ('value', True)
        ])
        
        s = session()
        tmpl = s.query(mgen.model.Template).filter_by(template_id=template_id).one()
        p = tmpl.project.get_permission(self.current_profile.email)
        if not p & Permission.Edit:
            raise mgen.error.Forbidden().describe("You cannot modify project " + proj_id)
        
        pk = self.request_params['pk']
        name = self.request_params['name']
        value = self.request_params['value']
        
        if pk == 'template':
            # modify template property
            log.debug('modify template "%s": %s=%s' % (template_id, name, value))
            setattr(tmpl, name, value)
            
        if 'template.params' in pk:
            # modify one of the params
            t, p, param_id = pk.split('.')
            log.debug('modify template parameter "%s.%s": %s=%s' % (template_id, param_id, name, value))
            for p in tmpl.params:
                if p['id'] == param_id:
                    p[name] = value
                    tmpl.params.changed()
                    break
            
        if 'erase.params' in pk:
            # remove one of the params
            e, p, param_id = pk.split('.')
            log.debug('remove template paramter "%s.%s"' % (template_id, param_id))
            params = copy.deepcopy(tmpl.params)
            tmpl.params.clear()
            for p in params:
                if p['id'] != param_id:
                    tmpl.params.append(p)
            tmpl.params.changed()
            
        if 'new.params' in pk:
            log.debug('new template parameter "%s.%s"' % (template_id, value['id']))
            tmpl.params.append(value)
            tmpl.params.changed()
        
        s.add(tmpl)
        self.commit_changes(s)
        return self.get_objects(mgen.model.Template,
                                self.query(),
                                tmpl.template_id)

class Items(GenericModelHandler):
    """Items restfull interface"""
    
    def query(self):
        return session().query(mgen.model.Item).join(mgen.model.Project,
                                                     mgen.model.Project.project_id==mgen.model.Item.project_id)\
                                               .join(mgen.model.project2profile,
                                                     mgen.model.project2profile.c.project==mgen.model.Project.project_id)\
                                               .join(mgen.model.Profile,
                                                     mgen.model.Profile.email==mgen.model.project2profile.c.profile)\
                                               .filter(mgen.model.Profile.email==self.current_profile.email)
    
    @authenticated
    @jsonify
    def get(self, item_id=None):
        """GET list or single project"""
        log.debug('REST GET %s -> %s' % (self.request.path,
            pprint.pformat(self.page_arguments) if item_id is None else '%s=%s' % (
                get_primary_key(mgen.model.Item), item_id) ))
        
        return self.get_objects(mgen.model.Item, self.query(), item_id)
        
    @authenticated
    @jsonify
    def post(self):
        """POST to create a new item"""
        log.debug("REST POST %s <- %s" % (self.request.path,
                                          pprint.pformat(self.request_params,
                                                         indent=2,
                                                         width=160)))
        self.validate_params([
            'name', 'type', 'body', 'published'
        ])
        def_uri_path = self.request_params['name'].lower().replace(' ', '-').replace('/', '-').replace('\\', '-')
        uri_path = self.request_params.get('uri_path', def_uri_path)
        
        itm = mgen.model.Item(item_id=uuid.uuid4(),
                              name=self.request_params['name'],
                              type=self.request_params['type'],
                              body=self.request_params['body'],
                              uri_path=uri_path,
                              published=self.request_params['published'])
        
        if itm.published:
            # publish it now
            itm.publish_date = datetime.datetime.now()
        elif 'publish_date' in self.request_params:
            itm.publish_date = datetime.datetime.strptime(self.request_params['publish_date'],
                                                          '%d-%m-%Y')
        else:
            raise mgen.error.BadRequest().describe('no value given for \
                publish_date and item is not published now')
        
        s = session()
        s.add(itm)
        
        if 'tags' in self.request_params:
            for tag in self.request_params['tags']:
                tag_name = tag.strip()
                log.debug('applying tag "%s" to item "%s"' % (tag_name, itm.item_id))
                atag = s.query(mgen.model.Tag).filter_by(tag=tag_name).first()
                if not atag:
                    log.debug(' -> it is a new tag, creating...')
                    atag = mgen.model.Tag(tag=tag_name)
                    s.add(atag)
                # append (new) tag to list of tags
                s.add(mgen.model.tag2item(tag=atag.tag, item=itm.item_id))

        self.commit_changes(s)
        self.set_status(201)
        
        log.debug('created new item: %s' % itm.item_id)
        return self.get_objects(mgen.model.Item, self.query(), itm.item_id)
        