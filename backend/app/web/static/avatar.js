/* Controlled-avatar renderer (RND-371).
 * The supplied URL is always an application endpoint. The fallback remains in
 * the DOM behind the image, so a 404/timeout/content failure never exposes a
 * browser broken-image glyph or shifts the surrounding layout. */
(function(window){
  'use strict';
  function initial(name){
    var value=(name==null?'':String(name)).trim();
    return value ? value.slice(0,1).toUpperCase() : '?';
  }
  function element(url,name,className){
    var root=document.createElement('span');
    root.className=(className||'avatar')+' avatar-controlled';
    root.setAttribute('role','img');
    root.setAttribute('aria-label',(name==null?'':String(name)).trim()||'?');
    var fallback=document.createElement('span');
    fallback.className='avatar-fallback';
    fallback.textContent=initial(name);
    root.appendChild(fallback);
    if(url){
      var image=document.createElement('img');
      image.className='avatar-image';
      image.src=String(url);
      image.alt='';
      image.loading='lazy';
      image.decoding='async';
      image.addEventListener('error',function(){
        image.remove();
        root.classList.add('avatar-fallback-only');
      },{once:true});
      root.appendChild(image);
    }else{
      root.classList.add('avatar-fallback-only');
    }
    return root;
  }
  function html(url,name,className){ return element(url,name,className).outerHTML; }
  window.ArchiveAvatar={element:element,html:html};
})(window);
