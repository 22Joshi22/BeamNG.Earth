// BeamNG.Earth data credits as a UI app (lua/ge/extensions/beamngEarth/hudapps.lua): the sources around the player.
angular.module('beamng.apps')
.directive('beamngEarthCredits', [function () {
  return {
    template:
      '<div class="bngApp" style="width:100%;height:100%;background:transparent;box-shadow:none;" layout="column" layout-align="end start">' +
        '<span ng-show="text && !hidden" style="background:rgba(10,12,16,0.45);color:rgba(238,241,244,0.85);border-radius:3px;' +
        'padding:2px 6px;font-size:11px;line-height:1.3;">{{ text }}</span>' +
      '</div>',
    replace: true,
    link: function (scope) {
      'use strict'
      scope.text = ''
      scope.hidden = false
      var hello = function (on) {
        bngApi.engineLua('if beamngEarth_core and beamngEarth_core.hudApp then beamngEarth_core.hudApp("credits", ' + on + ') end')
      }
      scope.$on('BeamNGEarthHud', function (event, data) {
        scope.$evalAsync(function () {
          scope.text = (data && data.credits) || ''
          scope.hidden = !!(data && data.hidden)
        })
      })
      hello(true)
      var again = setInterval(function () { hello(true) }, 3000)
      scope.$on('$destroy', function () { clearInterval(again); hello(false) })
    }
  }
}]);
