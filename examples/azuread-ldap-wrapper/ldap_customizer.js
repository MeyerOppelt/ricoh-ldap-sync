'use strict';

// Customizer for AzureAD-LDAP-wrapper, used with ricoh-ldap-sync. Mount it at
// /app/customizer/ldap_customizer.js; the wrapper loads it at startup.
//
// - Adds each user's Entra "Company name" (Graph companyName) as the LDAP
//   attribute "company", for ricoh-ldap-sync's LDAP_ATTR_COMPANY.
// - For guests whose Entra "mail" is empty, uses their real external address
//   from otherMails, never their #EXT# user principal name.

var customizer = {};

// Ask Graph for the extra properties. Only the $select list changes, so the
// wrapper's own filters (GRAPH_FILTER_USERS) stay as they are.
customizer.modifyGraphApiConfig = function (apiConfig, MS_GRAPH_SCOPE) {
    ['companyName', 'otherMails'].forEach(function (property) {
        if (!new RegExp('[=,]' + property + '(,|&|$)').test(apiConfig.uri)) {
            apiConfig.uri = apiConfig.uri.replace('$select=', '$select=' + property + ',');
        }
    });
    return apiConfig;
};

function isAddress(value) {
    return typeof value === 'string' && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value) && value.indexOf('#EXT#') === -1;
}

customizer.ModifyLDAPUser = function (ldapuser, azureuser) {
    if (typeof azureuser.companyName === 'string' && azureuser.companyName.trim() !== '') {
        ldapuser.company = azureuser.companyName.trim();
    }
    if (!isAddress(ldapuser.mail) && Array.isArray(azureuser.otherMails)) {
        var real = azureuser.otherMails.find(isAddress);
        if (real) ldapuser.mail = real;
    }
    return ldapuser;
};

module.exports = customizer;
