'use strict';
var React = require('react');
var globals = require('./globals');

var Home = module.exports.Home = React.createClass({
    render: function() {
        return (
            <div>
                <div className="homepage-main-box panel-gray">
                    <div className="row">
                        <div className="col-sm-12">
                            <div className="project-info site-title">
                                <h1>Snowflakes: Where every object is unique</h1>
                            </div>
                            <div id="info-box" className="project-info text-panel">
                                <h4>Snowflake Portal</h4>
                                <p>Enter a search term in the toolbar above.</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        );
    }
});


globals.content_views.register(Home, 'Portal');
